"""Plan, provision or verify explicitly selected owned Linux guests."""

import argparse
import json
import math
import os
import re
import select
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


def request_for(host, revision):
    fields = set(guest_host.BASELINE_FIELDS) | {
        "profile",
        "ansible_host",
        "cpu",
        "template_vmid",
        "proxmox_node",
        "storage",
        "bridge",
        "vlan",
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
        "template": templates.request_for(host, False, False),
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


def verify_guest(request, known_hosts, fresh):
    prepare_known_hosts(known_hosts, fresh)
    argv = guest_ssh(request["host"], known_hosts, fresh)
    deadline = time.monotonic() + (600 if fresh else 30)
    while True:
        try:
            probe = subprocess.run(argv + ["true"], capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValidationError(
                "Guest SSH unavailable/timed out; inspect first-boot network"
            ) from None
        if probe.returncode == 0:
            break
        require(
            time.monotonic() < deadline,
            "Guest SSH",
            "readiness deadline expired; inspect network/keys",
        )
        time.sleep(2)
    # Only enrollment uses accept-new; the actual privileged operation is strictly pinned.
    argv = guest_ssh(request["host"], known_hosts, False)
    source = (ROOT / "scripts/guest_verify.py").read_text()
    try:
        result = subprocess.run(
            argv + ["sudo -n python3 -c " + shlex.quote(source)],
            input=json.dumps({"request": request, "fresh": fresh}),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValidationError(
            "Guest verification timed out; inspect cloud-init and management state"
        ) from None
    require(
        result.returncode == 0, "Guest baseline", "verification failed; inspect private guest state"
    )
    require(
        len(result.stdout) <= 65536 and not result.stderr.strip(),
        "Guest baseline",
        "unexpected output; inspect private guest state",
    )
    try:
        observed = json.loads(result.stdout)
        guest_verify.validate_observation(request, observed)
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
        },
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
    return {
        key: observed[key]
        for key in (
            "os_id",
            "release",
            "architecture",
            "cpus",
            "memory_bytes",
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
        "prepared": {"fresh"},
        "ready": {"action", "config_sha256", "duration_seconds"},
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
    else:
        allowed = {"preserved", "would-create"} if phase == "planned" else {"preserved", "created"}
        require(event["action"] in allowed, "Native guest", "invalid action result")
    if phase == "ready":
        require(
            isinstance(event["config_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", event["config_sha256"])
            and type(event["duration_seconds"]) in {int, float}
            and math.isfinite(event["duration_seconds"])
            and event["duration_seconds"] >= 0,
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


def dispatch(requests, mode, known_hosts):
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
                    (json.dumps({"requests": requests, "mode": mode}) + "\n").encode()
                )
                process.stdin.flush()
                outcomes = []
                for request in requests:
                    event = read_event(process)
                    validate_event(request, event, "planned" if mode == "plan" else "prepared")
                    observations = {}
                    if mode != "plan":
                        require(
                            not event["fresh"] or mode == "apply",
                            "Native guest",
                            "unexpected mutation",
                        )
                        observations = verify_guest(request, known_hosts, event["fresh"])
                        ack = {
                            "vmid": event["vmid"],
                            "identity": event["identity"],
                            "verified": True,
                        }
                        process.stdin.write((json.dumps(ack) + "\n").encode())
                        process.stdin.flush()
                        event = read_event(process)
                        validate_event(request, event, "ready")
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
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        require(args.targets is not None, "targets", "select explicit comma-separated aliases")
        require(not args.apply or not args.verify, "operation", "verify does not accept apply")
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
            templates.api_admission(host)
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
            groups.setdefault(key, []).append(request_for(host, revision))
        mode = "verify" if args.verify else "apply" if args.apply else "plan"
        # Admit every selected host before the first mutating batch; each apply rechecks under lock.
        if mode == "apply":
            for requests in groups.values():
                dispatch(requests, "plan", pins)
        for requests in groups.values():
            for result in dispatch(requests, mode, pins):
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
