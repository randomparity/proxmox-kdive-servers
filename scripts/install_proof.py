"""Restore one clean-only guest, run the pinned KDIVE proof, and restore again."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import guest_host, guest_verify, guests, templates
    from .validate_inventory import ROOT, ValidationError, load_inventory, managed_hosts, require
else:
    import guest_host
    import guest_verify
    import guests
    import templates
    from validate_inventory import ROOT, ValidationError, load_inventory, managed_hosts, require

RUN_TIMEOUT = 24 * 3600
FAMILIES = {"ubuntu": "debian", "fedora": "fedora", "rocky": "enterprise"}
ERRORS = (ValidationError, guest_verify.GuestError, OSError, ValueError, subprocess.SubprocessError)


def exit_status(runner_code, reset_verified):
    return runner_code if runner_code else 0 if runner_code == 0 and reset_verified else 1


def private_directory(path):
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "proof output",
        "require an owned private directory (mode 0700)",
    )


def write_record(path, value):
    with path.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def digest_file(path):
    require(stat.S_ISREG(path.lstat().st_mode), "proof evidence", "refuse non-regular files")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def preflight(args):
    require(args.targets and "," not in args.targets, "targets", "select exactly one alias")
    require(
        not args.apply or args.confirm == args.targets and args.exclusive,
        "proof",
        "apply requires CONFIRM matching exact TARGETS and EXCLUSIVE=1",
    )
    data = load_inventory(args.inventory)
    guests.validate_inventory(data, args.targets)
    host = managed_hosts(data)[args.targets]
    require(host["profile"] in FAMILIES, "proof", "select Ubuntu, Fedora or Rocky")
    pins = Path(args.inventory).absolute().parent / "known_hosts"
    guests.prepare_known_hosts(pins, False)
    source = json.loads((ROOT / "vars/kdive-source.json").read_text())
    guest_verify.kdive_inputs(source)
    require(
        args.kdive_checkout and args.kernel_bundle and args.guest_image and args.output,
        "proof inputs",
        "set KDIVE_CHECKOUT, KERNEL_BUNDLE, GUEST_IMAGE and OUTPUT",
    )
    checkout = Path(args.kdive_checkout).resolve(strict=True)
    head = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(checkout), "status", "--porcelain"], text=True
    )
    require(head == source["commit"] and not dirty, "KDIVE checkout", "require clean pinned HEAD")
    python = checkout / ".venv/bin/python"
    subprocess.run(
        [str(python), "-m", "scripts.host_install_proof", "run", "--help"],
        cwd=checkout,
        check=True,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bundle = Path(args.kernel_bundle).resolve(strict=True)
    for name in ("manifest.json", "effective_config", "kernel.tar.gz"):
        require((bundle / name).is_file(), "kernel bundle", "use an existing upstream bundle")
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9._-]*", args.guest_image),
        "guest image",
        "use an upstream catalog name",
    )
    output = Path(args.output).absolute()
    reports = ROOT / "reports"
    require(
        output == output.resolve() and output.is_relative_to(reports) and output != reports,
        "proof output",
        "select a new directory under this checkout's ignored reports/",
    )
    require(not output.exists(), "proof output", "select a new run directory")
    if reports.exists():
        private_directory(reports)
    require(output.parent == reports, "proof output", "use reports/<new-run-name>")
    operator = None
    if args.operator_prerequisites:
        operator = Path(args.operator_prerequisites).read_bytes()
        operator.decode("utf-8")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    request = guests.request_for(host, revision, templates.api_admission(host, source=True))
    return request, pins, checkout, bundle, output, source["commit"], operator


def ssh_environment(host, pins, directory):
    """Adapt the upstream SSH/SCP CLI to the inventory's existing strict transport."""
    directory.mkdir(mode=0o700)
    config = directory / "config"
    lines = [
        "Host *",
        "  GlobalKnownHostsFile /dev/null",
        "  UpdateHostKeys no",
        "  StrictHostKeyChecking yes",
        "  BatchMode yes",
        "  " + guests.known_hosts_option(pins),
    ]
    key = host.get("ansible_ssh_private_key_file")
    if key:
        value = str(Path(key).expanduser().resolve(strict=True))
        require(
            "${" not in value and not any(c in value for c in "\r\n\0"),
            "SSH identity",
            "invalid key path",
        )
        value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        lines += ['  IdentityFile "' + value + '"', "  IdentitiesOnly yes"]
    config.write_text("\n".join(lines) + "\n")
    config.chmod(0o600)
    for tool in ("ssh", "scp"):
        executable = shutil.which(tool)
        require(executable and Path(executable).is_absolute(), "proof transport", "install SSH/SCP")
        launcher = directory / tool
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join([executable, "-F", str(config), "-o", guests.known_hosts_option(pins)])
            + ' "$@"\n'
        )
        launcher.chmod(0o700)
    return os.environ | {"PATH": str(directory) + os.pathsep + os.environ.get("PATH", "")}


class InterruptedProof(Exception):
    pass


class Interrupts:
    def __init__(self):
        self.signum = None
        self.running = False

    def handle(self, signum, _frame):
        self.signum = self.signum or signum
        if self.running:
            raise InterruptedProof

    @contextlib.contextmanager
    def installed(self):
        previous = {s: signal.signal(s, self.handle) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield self
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def run_runner(argv, checkout, env, output, interrupts):
    if interrupts.signum:
        return 128 + interrupts.signum
    with (output / "runner.log").open("wb") as log:
        os.fchmod(log.fileno(), 0o600)
        with subprocess.Popen(
            argv, cwd=checkout, env=env, stdout=log, stderr=log, start_new_session=True
        ) as process:
            try:
                interrupts.running = True
                if interrupts.signum:
                    raise InterruptedProof
                code = process.wait(timeout=RUN_TIMEOUT)
                return code if code >= 0 else 128 - code
            except (InterruptedProof, subprocess.TimeoutExpired):
                interrupts.running = False
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                return 128 + interrupts.signum if interrupts.signum else 124
            finally:
                interrupts.running = False


def acknowledge(process, request, phase, verified=True):
    process.stdin.write(
        (
            json.dumps(
                {
                    "vmid": request["host"]["vmid"],
                    "identity": guest_host.identity(request),
                    "phase": phase,
                    "verified": verified,
                }
            )
            + "\n"
        ).encode()
    )
    process.stdin.flush()


def restored_event(process, request, pins):
    event = guests.read_event(process)
    require(isinstance(event, dict), "proof event", "invalid native object")
    if event.get("phase") != "prepared":
        return event
    try:
        guests.validate_event(request, event, "prepared")
        require(event["fresh"] is False, "proof restore", "unexpected fresh guest")
        guests.verify_guest(request, pins, False, event["guest_uuid"], booting=True)
    except ERRORS:
        acknowledge(process, request, "prepared", False)
        return guests.read_event(process)
    acknowledge(process, request, "prepared")
    return guests.read_event(process)


def exchange(process, request, pins, runner, record):
    event = restored_event(process, request, pins)
    if event.get("phase") == "ready":
        try:
            guests.validate_event(request, event, "ready")
            require(event["action"] == "restored", "proof baseline", "restore not established")
            record["initial"] = event
            record["runner_exit_code"] = runner()
        except ERRORS:
            record["orchestration_error"] = "initial verification or runner launch failed"
        finally:
            acknowledge(process, request, "proof")
            event = guests.read_event(process)
    require(
        isinstance(event, dict)
        and set(event) == {"vmid", "identity", "phase", "initial_ok"}
        and event["phase"] == "proof-reset"
        and type(event["initial_ok"]) is bool
        and event["vmid"] == request["host"]["vmid"]
        and event["identity"] == guest_host.identity(request),
        "proof reset",
        "invalid reset transition",
    )
    if record["initial"] and not event["initial_ok"]:
        record["orchestration_error"] = "native proof acknowledgement failed"
    final = restored_event(process, request, pins)
    guests.validate_event(request, final, "ready")
    require(final["action"] == "restored", "proof reset", "restore not established")
    if record["initial"]:
        require(
            all(
                final[key] == record["initial"][key]
                for key in ("snapshot_identity", "snapshot_config_sha256", "snapshot_time")
            ),
            "proof reset",
            "clean snapshot changed during proof",
        )
    record["final"] = final
    record["reset_verified"] = True


def execute(request, pins, runner, record):
    with subprocess.Popen(
        guests.host_ssh(request["host"]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    ) as process:
        try:
            process.stdin.write(
                (
                    json.dumps(
                        {
                            "requests": [request],
                            "mode": "proof",
                            "confirmed": True,
                            "exclusive": True,
                            "level": "clean",
                            "capture": False,
                        }
                    )
                    + "\n"
                ).encode()
            )
            process.stdin.flush()
            exchange(process, request, pins, runner, record)
            process.stdin.close()
            require(
                process.wait(timeout=30) == 0 and not process.stdout.read(1),
                "proof transport",
                "native session did not finish cleanly",
            )
        finally:
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)


def apply(prepared, args):
    request, pins, checkout, bundle, output, candidate, operator = prepared
    output.parent.mkdir(mode=0o700, exist_ok=True)
    private_directory(output.parent)
    output.mkdir(mode=0o700)
    record = {
        "schema": 1,
        "candidate_sha": candidate,
        "controller_revision": request["revision"],
        "initial": None,
        "final": None,
        "runner_exit_code": None,
        "reset_verified": False,
        "operator_sha256": hashlib.sha256(operator).hexdigest() if operator is not None else None,
    }
    argv = [
        str(checkout / ".venv/bin/python"),
        "-m",
        "scripts.host_install_proof",
        "run",
        "--target",
        request["host"]["ansible_user"] + "@" + request["host"]["ansible_host"],
        "--known-hosts",
        str(pins),
        "--family",
        FAMILIES[request["host"]["profile"]],
        "--candidate",
        candidate,
        "--bundle",
        str(bundle),
        "--guest-image",
        args.guest_image,
        "--output",
        str(output / "upstream"),
    ]
    if operator is not None:
        script = output / "operator-prerequisites.sh"
        script.write_bytes(operator)
        script.chmod(0o600)
        argv += ["--operator-prerequisites", str(script)]
    env = ssh_environment(request["host"], pins, output / "ssh")
    with Interrupts().installed() as interrupts:
        try:

            def runner():
                write_record(output / "provenance.json", record)
                return run_runner(argv, checkout, env, output, interrupts)

            execute(request, pins, runner, record)
        except ERRORS:
            record["orchestration_error"] = "operation failed; inspect private evidence and guest"
        finally:
            record["interrupted_by"] = interrupts.signum
            record["evidence_sha256"] = {}
            try:
                for path in sorted((output / "upstream").rglob("*")):
                    require(not path.is_symlink(), "proof evidence", "symlink refused")
                    if path.is_file():
                        record["evidence_sha256"][str(path.relative_to(output))] = digest_file(path)
                    else:
                        require(path.is_dir(), "proof evidence", "non-regular entry refused")
            except ERRORS:
                record["evidence_error"] = "could not bind complete retained evidence"
            write_record(output / "provenance.json", record)
    print(
        json.dumps(
            {
                "runner_exit_code": record["runner_exit_code"],
                "reset_verified": record["reset_verified"],
                "evidence_complete": "evidence_error" not in record,
            }
        )
    )
    code = exit_status(record["runner_exit_code"], record["reset_verified"])
    if not code and interrupts.signum:
        return 128 + interrupts.signum
    return code or (1 if "orchestration_error" in record or "evidence_error" in record else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in [
        ("inventory", str(ROOT / "inventory/example.yml")),
        ("targets", None),
        ("confirm", None),
        ("kdive-checkout", None),
        ("kernel-bundle", None),
        ("guest-image", None),
        ("output", None),
        ("operator-prerequisites", None),
    ]:
        parser.add_argument(
            "--" + name, default=os.environ.get(name.upper().replace("-", "_"), default)
        )
    for name in ("apply", "exclusive"):
        parser.add_argument(
            "--" + name, action="store_true", default=os.environ.get(name.upper()) == "1"
        )
    args = parser.parse_args(argv)
    try:
        prepared = preflight(args)
        request, pins = prepared[:2]
        guests.dispatch([request], "plan-proof", pins)
        if args.apply:
            return apply(prepared, args)
        print(json.dumps({"action": "would-run-install-proof", "candidate_sha": prepared[5]}))
        return 0
    except ValidationError as error:
        print(f"Installation proof failed: {error}", file=sys.stderr)
        return 1
    except ERRORS:
        print(
            "Installation proof failed: inspect inputs, pinned checkout and private guest state",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
