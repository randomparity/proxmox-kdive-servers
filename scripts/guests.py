"""Plan, provision or verify explicitly selected owned Linux guests."""

import argparse
import json
import math
import os
import re
import select
import selectors
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from . import guest_host, guest_verify, templates
    from .validate_inventory import (
        ROOT,
        ValidationError,
        load_inventory,
        managed_hosts,
        require,
        validate_inventory,
    )
else:
    import guest_host
    import guest_verify
    import templates
    from validate_inventory import (
        ROOT,
        ValidationError,
        load_inventory,
        managed_hosts,
        require,
        validate_inventory,
    )


REBOOT_DISCONNECTED = object()


def request_for(host, revision, source):
    require(isinstance(source, dict), "source template", "missing authenticated configuration")
    template = guest_host.template_host.source_request(
        templates.request_for(host, False, False), source
    )
    require(
        source.get("template") == 1
        and source.get("description")
        == "kdive-template-v1:" + guest_host.template_host.identity(template) + ":ready",
        "source template",
        "ownership differs; inspect immutable template inputs",
    )
    fields = set(guest_host.BASELINE_FIELDS) | {
        "profile",
        "ansible_host",
        "cpu",
        "template_vmid",
        "proxmox_node",
        "storage",
        "bridge",
        "vlan",
        "nic_model",
        "nic_queues",
        "balloon_mib",
        "proxmox_api_host",
        "proxmox_api_port",
        "proxmox_ssh_host",
        "proxmox_ssh_user",
        "proxmox_ssh_port",
        "api_user_env",
        "api_token_id_env",
        "api_token_secret_env",
        "proxmox_api_ca_file",
        "proxmox_ssh_private_key_file",
        "ansible_ssh_private_key_file",
    }
    return {
        "host": {k: v for k, v in host.items() if k in fields},
        "template": template,
        "revision": revision,
    }


def prepare_known_hosts(path, fresh):
    info = path.parent.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "SSH pins",
        "keep the inventory directory private (mode 0700) and owned by this user",
    )
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if fresh else 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "r+") as handle:
            info = os.fstat(handle.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1,
                "SSH pins",
                "require a private regular mode-0600 known_hosts file",
            )
    except OSError:
        raise ValidationError(
            "SSH pins: unavailable; inspect private inventory known_hosts"
        ) from None


def known_hosts_option(path):
    value = str(path)
    require(
        path.is_absolute() and "${" not in value and not any(c in value for c in "\r\n\0"),
        "SSH pins",
        "use an absolute inventory path without environment expansion, line breaks or NUL",
    )
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return 'UserKnownHostsFile="' + value + '"'


def guest_ssh(host, known_hosts, fresh):
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=" + ("accept-new" if fresh else "yes"),
        "-o",
        known_hosts_option(known_hosts),
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-l",
        host["ansible_user"],
    ]
    if host.get("ansible_ssh_private_key_file"):
        argv += [
            "-i",
            str(Path(host["ansible_ssh_private_key_file"]).expanduser()),
            "-o",
            "IdentitiesOnly=yes",
        ]
    return argv + ["--", host["ansible_host"]]


def run_guest(argv, *, input=None, timeout):
    deadline = time.monotonic() + timeout
    pending = memoryview(input.encode() if input is not None else b"")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    received = 0
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        with selectors.DefaultSelector() as selector:
            for name in output:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        try:
                            sent = os.write(key.fd, pending[:4096])
                            pending = pending[sent:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, min(4096, 65537 - received))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    received += len(chunk)
                    require(
                        received <= 65536,
                        "Guest SSH",
                        "response exceeds limit; inspect private guest state without retry",
                    )
                    output[key.data].extend(chunk)
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
        try:
            return subprocess.CompletedProcess(
                argv, code, output["stdout"].decode("utf-8"), output["stderr"].decode("utf-8")
            )
        except UnicodeError:
            raise ValidationError("Guest SSH: invalid response encoding") from None
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def guest_rpc(request, known_hosts, envelope, timeout):
    argv = guest_ssh(request["host"], known_hosts, False)
    source = (ROOT / "scripts/guest_verify.py").read_text()
    try:
        result = run_guest(
            argv + ["sudo -n python3 -c " + shlex.quote(source)],
            input=json.dumps(dict(envelope, request=request)),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValidationError(
            "Guest RPC timed out; inspect retained partial without retry"
        ) from None
    reboot_disconnect = set(envelope) == {"reboot_from"} and result.returncode == 255
    if reboot_disconnect and not result.stdout:
        return REBOOT_DISCONNECTED
    require(
        (result.returncode == 0 or reboot_disconnect)
        and len(result.stdout) <= 65536
        and (reboot_disconnect or not result.stderr.strip()),
        "Guest baseline",
        "operation failed; inspect private guest state",
    )
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ValidationError("Guest baseline: invalid result") from None


def wait_guest(request, known_hosts, fresh, after_boot, booting=False):
    argv = guest_ssh(request["host"], known_hosts, fresh)
    deadline = time.monotonic() + (600 if fresh or after_boot or booting else 30)
    while True:
        try:
            probe = run_guest(
                argv + ["cat /proc/sys/kernel/random/boot_id" if after_boot else "true"],
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValidationError(
                "Guest SSH unavailable/timed out; inspect guest network"
            ) from None
        if probe.returncode == 0:
            if not after_boot or guest_verify.machine_uuid(probe.stdout) != after_boot:
                return
        require(
            time.monotonic() < deadline,
            "Guest SSH",
            "readiness deadline expired; inspect network/keys/boot without retrying reboot",
        )
        time.sleep(2)


def verify_guest(request, known_hosts, fresh, guest_uuid, after_boot=None, booting=False):
    request = dict(request, guest_uuid=guest_verify.machine_uuid(guest_uuid))
    prepare_known_hosts(known_hosts, fresh)
    if booting:
        wait_guest(request, known_hosts, fresh, after_boot, booting=True)
    else:
        wait_guest(request, known_hosts, fresh, after_boot)
    observed = guest_rpc(request, known_hosts, {"fresh": fresh}, 1800)
    try:
        guest_verify.validate_observation(request, observed)
        if after_boot:
            require(observed["boot_id"] != after_boot, "Guest boot", "reboot not established")
    except (ValueError, TypeError, KeyError):
        raise ValidationError("Guest baseline: invalid or mismatched result") from None
    packages = observed.get("packages")
    require(
        isinstance(packages, dict)
        and set(packages)
        == {
            "cloud-init",
            "openssh-server",
            "sudo",
            "qemu-guest-agent",
            "python3-base" if request["host"]["profile"] == "opensuse" else "python3",
        }
        | (
            set(guest_verify.OPENSUSE_PACKAGES)
            if request["host"]["profile"] == "opensuse"
            else set()
        ),
        "Guest baseline",
        "management package evidence incomplete",
    )
    require(
        all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9.+:~_%-]{1,180}", v)
            for v in packages.values()
        ),
        "Guest baseline",
        "invalid package version evidence",
    )
    if request["host"]["profile"] == "opensuse":
        require(
            all(
                packages[name] == "0:" + version
                for name, version in guest_verify.OPENSUSE_PACKAGES.items()
            ),
            "Guest baseline",
            "pinned kernel prerequisite evidence differs",
        )
    return {
        key: observed[key]
        for key in (
            "boot_id",
            "os_id",
            "release",
            "architecture",
            "cpus",
            "memory_bytes",
            "crash_reserved_bytes",
            "balloon_driver",
            "filesystem_bytes",
            "security",
            "kvm_api",
            "kvm_create_vm",
            "packages",
        )
    }


def host_source():
    sources = {
        name: (ROOT / f"scripts/{name}.py").read_text()
        for name in ("validate_inventory", "template_host", "guest_verify")
    }
    bootstrap = "import sys,types,json\n"
    for name, source in sources.items():
        bootstrap += (
            f"m=types.ModuleType({name!r});m.__file__='/kdive/{name}.py';"
            f"sys.modules[{name!r}]=m;exec({source!r},m.__dict__)\n"
        )
    return bootstrap + (ROOT / "scripts/guest_host.py").read_text()


def host_ssh(host):
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(host.get("proxmox_ssh_port", 22)),
        "-l",
        host["proxmox_ssh_user"],
    ]
    if host.get("proxmox_ssh_private_key_file"):
        argv += [
            "-i",
            str(Path(host["proxmox_ssh_private_key_file"]).expanduser()),
            "-o",
            "IdentitiesOnly=yes",
        ]
    return argv + ["--", host["proxmox_ssh_host"], "python3 -u -c " + shlex.quote(host_source())]


def validate_event(request, event, phase):
    keys = {"vmid", "identity", "phase"} | {
        "planned": {"action"},
        "prepared": {"fresh", "guest_uuid"},
        "reboot": {"guest_uuid"},
        "baseline-boot": {"guest_uuid"},
        "removed": {"action", "duration_seconds"},
        "ready": {
            "action",
            "config_sha256",
            "duration_seconds",
            "snapshot",
            "snapshot_identity",
            "snapshot_config_sha256",
            "snapshot_time",
        },
    }[phase]
    require(
        isinstance(event, dict)
        and set(event) == keys
        and type(event["vmid"]) is int
        and event["vmid"] == request["host"]["vmid"]
        and event["identity"] == guest_host.identity(request)
        and event["phase"] == phase,
        "Native guest",
        "result identity/shape mismatch; inspect selected resources",
    )
    if phase == "prepared":
        require(type(event["fresh"]) is bool, "Native guest", "invalid preparation phase")
        guest_verify.machine_uuid(event["guest_uuid"])
    elif phase in {"reboot", "baseline-boot"}:
        guest_verify.machine_uuid(event["guest_uuid"])
    else:
        allowed = {
            "planned": {"preserved", "would-create", "would-restore", "would-destroy", "absent"},
            "ready": {"preserved", "created", "restored"},
            "removed": {"destroyed", "absent"},
        }[phase]
        require(event["action"] in allowed, "Native guest", "invalid action result")
    if phase in {"ready", "removed"}:
        require(
            type(event["duration_seconds"]) in {int, float}
            and math.isfinite(event["duration_seconds"])
            and event["duration_seconds"] >= 0,
            "Native guest",
            "invalid duration",
        )
    if phase == "ready":
        require(
            event["snapshot"] == "clean"
            and type(event["snapshot_time"]) is int
            and event["snapshot_time"] > 0
            and all(
                isinstance(event[key], str) and re.fullmatch(r"[a-f0-9]{64}", event[key])
                for key in ("snapshot_identity", "snapshot_config_sha256")
            ),
            "Native guest",
            "invalid snapshot evidence",
        )
        require(
            isinstance(event["config_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", event["config_sha256"]),
            "Native guest",
            "invalid outcome evidence",
        )


def read_event(process):
    deadline = time.monotonic() + 2400
    line = bytearray()
    while not line.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        require(
            remaining > 0 and select.select([process.stdout], [], [], remaining)[0],
            "Native guest",
            "operation deadline expired; inspect partial allocation",
        )
        part = os.read(process.stdout.fileno(), 1)
        require(
            part and len(line) < 65536,
            "Native guest",
            "transport ended or output exceeded bound; inspect state",
        )
        line.extend(part)
    try:
        event = json.loads(line)
    except ValueError:
        raise ValidationError("Native guest: invalid result; inspect private host state") from None
    require(
        not isinstance(event, dict) or "error" not in event,
        "Native guest",
        "operation failed; inspect ownership, configuration and prerequisites",
    )
    return event


def complete_guest(process, request, event, mode, known_hosts):
    require(not event["fresh"] or mode == "apply", "Native guest", "unexpected mutation")
    guest_uuid, fresh = event["guest_uuid"], event["fresh"]
    observations = verify_guest(request, known_hosts, fresh, guest_uuid, booting=mode == "restore")
    ack = {
        "vmid": event["vmid"],
        "identity": event["identity"],
        "verified": True,
        "phase": "prepared",
    }
    process.stdin.write((json.dumps(ack) + "\n").encode())
    process.stdin.flush()
    event = read_event(process)
    if fresh and request["host"]["profile"] == "opensuse":
        validate_event(request, event, "reboot")
        require(event["guest_uuid"] == guest_uuid, "Native guest", "reboot UUID changed")
        boot = observations["boot_id"]
        result = guest_rpc(
            dict(request, guest_uuid=guest_uuid), known_hosts, {"reboot_from": boot}, 60
        )
        require(
            result is REBOOT_DISCONNECTED or result == {"reboot_requested": True},
            "Guest reboot",
            "invalid acknowledgement",
        )
        observations = verify_guest(request, known_hosts, False, guest_uuid, after_boot=boot)
        process.stdin.write((json.dumps(dict(ack, phase="post-reboot")) + "\n").encode())
        process.stdin.flush()
        event = read_event(process)
    if fresh:
        validate_event(request, event, "baseline-boot")
        require(event["guest_uuid"] == guest_uuid, "Native guest", "baseline boot UUID changed")
        observations = verify_guest(
            request, known_hosts, False, guest_uuid, after_boot=observations["boot_id"]
        )
        process.stdin.write((json.dumps(dict(ack, phase="baseline-boot")) + "\n").encode())
        process.stdin.flush()
        event = read_event(process)
    validate_event(request, event, "ready")
    observations.pop("boot_id")
    return event, observations


def dispatch(requests, mode, known_hosts, confirmed=False, exclusive=False):
    argv = host_ssh(requests[0]["host"])
    try:
        with subprocess.Popen(
            argv,
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
                                "requests": requests,
                                "mode": mode,
                                "confirmed": confirmed,
                                "exclusive": exclusive,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                process.stdin.flush()
                outcomes = []
                for request in requests:
                    event = read_event(process)
                    if mode.startswith("plan"):
                        phase = "planned"
                    elif mode == "teardown":
                        phase = "removed"
                    else:
                        phase = "prepared"
                    validate_event(request, event, phase)
                    observations = {}
                    if phase == "prepared":
                        event, observations = complete_guest(
                            process, request, event, mode, known_hosts
                        )
                    require(
                        event["action"]
                        in {
                            "plan": {"preserved", "would-create"},
                            "plan-restore": {"would-restore"},
                            "plan-teardown": {"would-destroy", "absent"},
                            "apply": {"created", "preserved"},
                            "verify": {"preserved"},
                            "restore": {"restored"},
                            "teardown": {"destroyed", "absent"},
                        }[mode],
                        "Native guest",
                        "action does not match requested operation",
                    )
                    outcomes.append(
                        {k: v for k, v in event.items() if k not in {"vmid", "phase"}}
                        | {
                            "profile": request["host"]["profile"],
                            "revision": request["revision"],
                            "source_identity": guest_host.template_host.identity(
                                request["template"]
                            ),
                        }
                        | observations
                    )
                process.stdin.close()
                require(
                    process.wait(timeout=30) == 0, "Native guest", "operation exited unsuccessfully"
                )
                require(not process.stdout.read(1), "Native guest", "unexpected trailing output")
                return outcomes
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise ValidationError(
            "Native guest transport failed; inspect selected partial resources"
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", default=os.environ.get("INVENTORY", ROOT / "inventory/example.yml")
    )
    parser.add_argument("--targets", default=os.environ.get("TARGETS"))
    parser.add_argument("--apply", action="store_true", default=os.environ.get("APPLY") == "1")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--verify", action="store_true")
    operation.add_argument("--restore", action="store_true")
    operation.add_argument("--teardown", action="store_true")
    parser.add_argument("--confirm", default=os.environ.get("CONFIRM"))
    parser.add_argument(
        "--exclusive", action="store_true", default=os.environ.get("EXCLUSIVE") == "1"
    )
    args = parser.parse_args()
    try:
        require(args.targets is not None, "targets", "select explicit comma-separated aliases")
        require(not args.apply or not args.verify, "operation", "verify does not accept apply")
        destructive = args.restore or args.teardown
        require(
            not (destructive and args.apply) or args.confirm == args.targets and args.exclusive,
            "operation",
            "restore/teardown apply requires CONFIRM matching exact TARGETS "
            "and EXCLUSIVE=1 after consumer release",
        )
        data = load_inventory(args.inventory)
        validate_inventory(data, args.targets)
        hosts = managed_hosts(data)
        pins = Path(args.inventory).absolute().parent / "known_hosts"
        known_hosts_option(pins)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        groups = {}
        for alias in args.targets.split(","):
            host = hosts[alias]
            source = templates.api_admission(host, source=True)
            key = tuple(
                host.get(k)
                for k in (
                    "proxmox_node",
                    "proxmox_ssh_host",
                    "proxmox_ssh_port",
                    "proxmox_ssh_user",
                    "proxmox_ssh_private_key_file",
                )
            )
            groups.setdefault(key, []).append(request_for(host, revision, source))
        selected_mode = "restore" if args.restore else "teardown"
        if destructive:
            mode = selected_mode if args.apply else "plan-" + selected_mode
        elif args.verify:
            mode = "verify"
        else:
            mode = "apply" if args.apply else "plan"
        # Admit every selected host before the first mutating batch; each apply rechecks under lock.
        if mode in {"apply", "restore", "teardown"}:
            for requests in groups.values():
                dispatch(requests, "plan-" + mode if destructive else "plan", pins)
        for requests in groups.values():
            for result in dispatch(
                requests,
                mode,
                pins,
                confirmed=args.confirm == args.targets,
                exclusive=args.exclusive,
            ):
                print(json.dumps(result, sort_keys=True), flush=True)
    except ValidationError as error:
        print(f"Guest operation failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        print(
            "Guest operation failed: invalid configuration or native result; inspect privately",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
