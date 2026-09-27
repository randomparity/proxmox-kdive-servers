"""Management-only preparation and reusable Linux guest baseline verification."""

import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path


class GuestError(ValueError):
    """A public-safe failed baseline contract."""


def check(condition, message):
    if not condition:
        raise GuestError(message)


def command(argv, timeout=60):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise GuestError(
            "Guest command unavailable/timed out; inspect management prerequisites"
        ) from None
    check(result.returncode == 0, "Guest command failed; inspect cloud-init, packages and services")
    return result.stdout


def kvm_probe():
    device = None
    machine = None
    try:
        device = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        version = fcntl.ioctl(device, 0xAE00, 0)
        check(
            version == 12, "KVM API differs; inspect installed kernel and virtualization exposure"
        )
        machine = fcntl.ioctl(device, 0xAE01, 0)
        return version
    except OSError:
        raise GuestError(
            "KVM API unavailable; inspect nesting and installed guest modules"
        ) from None
    finally:
        if machine is not None:
            os.close(machine)
        if device is not None:
            os.close(device)


def prepare(profile):
    check(
        all(shutil.which(tool) for tool in ("cloud-init", "sudo", "sshd", "python3")),
        "Pinned management prerequisites missing; inspect image identity",
    )
    if not shutil.which("qemu-ga"):
        if profile == "ubuntu":
            command(["apt-get", "update"], timeout=600)
            command(
                ["apt-get", "install", "-y", "--no-install-recommends", "qemu-guest-agent"],
                timeout=600,
            )
        else:
            raise GuestError(
                "Pinned guest agent missing; inspect image identity before preparation"
            )
    command(["systemctl", "enable", "--now", "qemu-guest-agent"], timeout=120)
    flags = Path("/proc/cpuinfo").read_text().split()
    module = "kvm_intel" if "vmx" in flags else "kvm_amd" if "svm" in flags else None
    check(module is not None, "Guest virtualization flags absent; inspect host CPU exposure")
    command(["modprobe", module])
    path = Path("/etc/modules-load.d/kdive-kvm.conf")
    check(
        not path.exists() or path.read_text() == module + "\n",
        "Guest module persistence differs; inspect fresh baseline",
    )
    path.write_text(module + "\n")
    path.chmod(0o644)


def security_state(profile):
    if profile == "ubuntu":
        check(
            Path("/sys/module/apparmor/parameters/enabled").read_text().strip() == "Y",
            "AppArmor disabled; preserve the image security baseline",
        )
        status = json.loads(command(["aa-status", "--json"]))
        check(
            isinstance(status.get("profiles"), dict) and "enforce" in status["profiles"].values(),
            "No enforcing AppArmor profiles; inspect security baseline",
        )
        return "apparmor-enforcing"
    check(
        Path("/sys/fs/selinux/enforce").read_text().strip() == "1",
        "SELinux not enforcing; inspect security baseline",
    )
    return "selinux-enforcing"


def package_versions(profile):
    names = [
        "cloud-init",
        "openssh-server",
        "sudo",
        "qemu-guest-agent",
        "python3-base" if profile == "opensuse" else "python3",
    ]
    versions = {}
    for name in names:
        argv = (
            ["dpkg-query", "-W", "-f=${Version}", name]
            if profile == "ubuntu"
            else ["rpm", "-q", "--qf", "%{EPOCHNUM}:%{VERSION}-%{RELEASE}", name]
        )
        version = command(argv).strip()
        check(
            bool(re.fullmatch(r"[A-Za-z0-9.+:~_%-]{1,180}", version)),
            "Invalid management package identity; inspect package database",
        )
        versions[name] = version
    return versions


def machine_uuid(value):
    check(isinstance(value, str), "Guest UUID missing; inspect owned VM identity")
    try:
        parsed = uuid.UUID(value.strip())
    except ValueError:
        raise GuestError("Guest UUID malformed; inspect owned VM identity") from None
    check(parsed.int != 0, "Guest UUID is empty; inspect owned VM identity")
    return str(parsed)


def identity_observation():
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID"}:
            release[key] = shlex.split(value)[0]
    interfaces = json.loads(command(["ip", "-j", "-4", "address", "show", "scope", "global"]))
    try:
        guest_uuid = machine_uuid(Path("/sys/class/dmi/id/product_uuid").read_text())
    except OSError:
        raise GuestError("Guest UUID unavailable; inspect owned VM identity") from None
    return {
        "guest_uuid": guest_uuid,
        "os_id": release.get("ID"),
        "release": release.get("VERSION_ID"),
        "architecture": platform.machine(),
        "hostname": socket.gethostname().split(".")[0],
        "fqdn": socket.getfqdn(),
        "ipv4": [
            address["local"]
            for interface in interfaces
            for address in interface.get("addr_info", [])
            if address.get("family") == "inet"
        ],
    }


def crash_reservation():
    try:
        value = Path("/sys/kernel/kexec_crash_size").read_text().strip()
    except FileNotFoundError:
        return 0
    check(bool(re.fullmatch(r"[0-9]{1,20}", value)), "Invalid native crash memory reservation")
    return int(value)


def observation(request):
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    filesystem = os.statvfs("/")
    return identity_observation() | {
        "cpus": os.cpu_count(),
        "memory_bytes": int(memory["MemTotal"].split()[0]) * 1024,
        "crash_reserved_bytes": crash_reservation(),
        "filesystem_bytes": filesystem.f_blocks * filesystem.f_frsize,
        "security": security_state(request["host"]["profile"]),
        "kvm_api": kvm_probe(),
        "kvm_create_vm": True,
    }


def validate_identity(request, result):
    host = request["host"]
    check(
        machine_uuid(result.get("guest_uuid")) == machine_uuid(request.get("guest_uuid")),
        "Guest UUID differs from owned native VM; inspect SSH destination before preparation",
    )
    expected_os = {
        "ubuntu": "ubuntu",
        "fedora": "fedora",
        "rocky": "rocky",
        "opensuse": "opensuse-leap",
    }[host["profile"]]
    check(
        result.get("os_id") == expected_os
        and result.get("release") == request["template"]["image"]["release"]
        and result.get("architecture") == "x86_64",
        "Guest OS/release/architecture differs",
    )
    check(
        result.get("hostname") == host["fqdn"].split(".")[0] and result.get("fqdn") == host["fqdn"],
        "Guest hostname/FQDN differs; inspect cloud-init",
    )
    check(host["ansible_host"] in result.get("ipv4", []), "Guest static IPv4 differs")


def validate_observation(request, result):
    validate_identity(request, result)
    host = request["host"]
    check(
        type(result.get("cpus")) is int and result["cpus"] == host["cores"],
        "Guest CPU count differs",
    )
    configured = host["memory_mib"] * 1024**2
    usable, reserved = result.get("memory_bytes"), result.get("crash_reserved_bytes")
    check(
        type(usable) is int
        and 0 < usable <= configured
        and type(reserved) is int
        and 0 <= reserved <= min(512 * 1024**2, configured // 4)
        and usable + reserved <= configured,
        "Guest memory evidence invalid; inspect usable RAM and native crash reservation",
    )
    check(
        usable + reserved >= configured * 0.9,
        "Guest memory capacity insufficient; inspect allocation and crash reservation",
    )
    disk = host["disk_gib"] * 1024**3
    check(
        type(result.get("filesystem_bytes")) is int
        and result["filesystem_bytes"] >= disk - max(2 * 1024**3, disk * 0.1),
        "Guest filesystem capacity insufficient; inspect first-boot growth",
    )
    security = "apparmor-enforcing" if host["profile"] == "ubuntu" else "selinux-enforcing"
    check(result.get("security") == security, "Guest security enforcement differs")
    check(
        result.get("kvm_api") == 12 and result.get("kvm_create_vm") is True,
        "Guest KVM capability not established",
    )


def cloud_init_ready():
    try:
        result = subprocess.run(
            ["cloud-init", "status", "--wait", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GuestError("Cloud-init status unavailable/timed out; inspect bootstrap") from None
    cloud = json.loads(result.stdout)
    check(isinstance(cloud, dict), "Invalid cloud-init status")
    notices = cloud.get("recoverable_errors", {})
    # Proxmox 9.2 generates scalar user data; accept only its verified advisory.
    advisory = (
        "'user' of type string is deprecated in 22.2 and scheduled to be removed in 27.2. "
        "Use 'users' list instead."
    )
    known_advisory = (
        isinstance(notices, dict)
        and set(notices) == {"DEPRECATED"}
        and (
            isinstance(notices["DEPRECATED"], list)
            and bool(notices["DEPRECATED"])
            and all(message == advisory for message in notices["DEPRECATED"])
        )
    )
    check(
        result.returncode in {0, 2}
        and not result.stderr.strip()
        and cloud.get("status") == "done"
        and cloud.get("errors", []) == []
        and (known_advisory or notices == {} and result.returncode == 0),
        "Cloud-init did not complete cleanly",
    )


def run(request, fresh=False):
    check(os.geteuid() == 0 and platform.system() == "Linux", "Verifier requires Linux and sudo")
    cloud_init_ready()
    validate_identity(request, identity_observation())
    if fresh:
        prepare(request["host"]["profile"])
    command(["systemctl", "is-active", "qemu-guest-agent"])
    result = observation(request)
    validate_observation(request, result)
    result["packages"] = package_versions(request["host"]["profile"])
    return result


def main():
    try:
        envelope = json.loads(sys.stdin.read(65537))
        check(
            set(envelope) == {"request", "fresh"} and type(envelope["fresh"]) is bool,
            "Invalid guest verification request",
        )
        print(json.dumps(run(envelope["request"], envelope["fresh"]), sort_keys=True))
    except (GuestError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            "Guest baseline failed; inspect private cloud-init, identity, sizing, security and KVM",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
