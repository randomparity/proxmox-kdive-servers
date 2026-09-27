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


def observation(request):
    host = request["host"]
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID"}:
            release[key] = shlex.split(value)[0]
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    filesystem = os.statvfs("/")
    interfaces = json.loads(command(["ip", "-j", "-4", "address", "show", "scope", "global"]))
    return {
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
        "cpus": os.cpu_count(),
        "memory_bytes": int(memory["MemTotal"].split()[0]) * 1024,
        "filesystem_bytes": filesystem.f_blocks * filesystem.f_frsize,
        "security": security_state(host["profile"]),
        "kvm_api": kvm_probe(),
        "kvm_create_vm": True,
    }


def validate_observation(request, result):
    host = request["host"]
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
    check(
        type(result.get("cpus")) is int and result["cpus"] == host["cores"],
        "Guest CPU count differs",
    )
    for field, minimum in (
        ("memory_bytes", host["memory_mib"] * 1024**2 * 0.9),
        (
            "filesystem_bytes",
            host["disk_gib"] * 1024**3 - max(2 * 1024**3, host["disk_gib"] * 1024**3 * 0.1),
        ),
    ):
        check(
            type(result.get(field)) is int and result[field] >= minimum,
            "Guest memory/filesystem capacity insufficient; inspect first-boot growth",
        )
    security = "apparmor-enforcing" if host["profile"] == "ubuntu" else "selinux-enforcing"
    check(result.get("security") == security, "Guest security enforcement differs")
    check(
        result.get("kvm_api") == 12 and result.get("kvm_create_vm") is True,
        "Guest KVM capability not established",
    )


def run(request, fresh=False):
    check(os.geteuid() == 0 and platform.system() == "Linux", "Verifier requires Linux and sudo")
    cloud = json.loads(command(["cloud-init", "status", "--wait", "--format", "json"], timeout=600))
    check(
        cloud.get("status") == "done"
        and not cloud.get("errors")
        and not cloud.get("recoverable_errors"),
        "Cloud-init did not complete cleanly",
    )
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
