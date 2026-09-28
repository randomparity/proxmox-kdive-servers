"""Management-only preparation and reusable Linux guest baseline verification."""

import fcntl
import hashlib
import json
import os
import platform
import pty
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import termios
import time
import uuid
import xml.etree.ElementTree as ET
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


OPENSUSE_KERNEL = "6.12.0-160000.38-default"
OPENSUSE_PACKAGES = {
    "kernel-default": "6.12.0-160000.38.1",
    "ucode-intel": "20260812-160000.1.1",
}
OPENSUSE_BASE = "6.12.0-160000.38.1.160000.2.24"


def validate_kernel_transaction(summary):
    check(
        summary.get("packages-to-change") == "3"
        and len(summary) == 2
        and {child.tag for child in summary} == {"to-install", "to-remove"},
        "Kernel transaction actions differ; inspect vendor repository state",
    )
    for action, expected, repository in (
        ("to-install", OPENSUSE_PACKAGES, "_tmpRPMcache_"),
        ("to-remove", {"kernel-default-base": OPENSUSE_BASE}, "@System"),
    ):
        packages = list(summary.find(action))
        check(
            len(packages) == len(expected)
            and {
                (
                    p.tag,
                    p.get("type"),
                    p.get("name"),
                    p.get("edition"),
                    p.get("arch"),
                    p.get("repository"),
                )
                for p in packages
            }
            == {
                ("solvable", "package", name, version, "x86_64", repository)
                for name, version in expected.items()
            },
            "Kernel transaction packages differ; inspect pinned prerequisite",
        )


def confirm_kernel_transaction(argv, timeout=900):
    parser = ET.XMLPullParser(events=("end",))
    deadline, size, reviewed, confirmed = time.monotonic() + timeout, 0, False, False
    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attributes)

    # Launch a fresh interpreter: Python preexec callbacks can deadlock after fork.
    attach = (
        "import fcntl,os,sys,termios;os.setsid();"
        "fcntl.ioctl(int(sys.argv[1]),termios.TIOCSCTTY,0);"
        "os.close(int(sys.argv[1]));os.execvp(sys.argv[2],sys.argv[2:])"
    )

    with os.fdopen(master, "wb", buffering=0) as terminal, os.fdopen(slave, "rb"):
        with subprocess.Popen(
            [sys.executable, "-c", attach, str(slave), *argv],
            stdin=subprocess.DEVNULL,
            pass_fds=(slave,),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=dict(os.environ, LC_ALL="C"),
        ) as process:
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    check(
                        remaining > 0 and select.select([process.stdout], [], [], remaining)[0],
                        "Kernel transaction timed out; inspect retained preparing guest",
                    )
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    size += len(chunk)
                    check(size <= 1048576, "Kernel transaction output exceeded bound")
                    parser.feed(chunk)
                    for _, element in parser.read_events():
                        check(
                            not (element.tag == "message" and element.get("type") == "error"),
                            "Kernel transaction reported an error; inspect retained partial",
                        )
                        if element.tag == "install-summary":
                            check(not reviewed, "Repeated kernel transaction summary")
                            validate_kernel_transaction(element)
                            reviewed = True
                        if element.tag == "prompt":
                            check(
                                reviewed and not confirmed and element.get("id") == "0",
                                "Unexpected kernel transaction prompt; no further confirmation",
                            )
                            terminal.write(b"y\n")
                            confirmed = True
                parser.close()
                check(
                    confirmed and process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                    "Kernel transaction failed; inspect retained preparing guest",
                )
            except (ET.ParseError, OSError, subprocess.SubprocessError):
                raise GuestError(
                    "Kernel transaction failed; inspect retained preparing guest"
                ) from None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def download_kernel_packages(directory):
    repository = "openSUSE:repo-oss"
    repos = ET.fromstring(command(["zypper", "--xmlout", "--no-refresh", "repos", "--details"]))
    selected = [p for p in repos.iter("repo") if p.get("alias") == repository]
    check(
        len(selected) == 1
        and all(selected[0].get(k) == "1" for k in ("enabled", "gpgcheck", "repo_gpgcheck")),
        "Vendor repository signature policy differs; inspect without weakening trust",
    )
    command(
        [
            "zypper",
            "--xmlout",
            "--non-interactive",
            "--no-refresh",
            "--pkg-cache-dir",
            directory,
            "download",
            "--repo",
            repository,
        ]
        + [name + "=" + version for name, version in OPENSUSE_PACKAGES.items()],
        timeout=600,
    )
    files = list(Path(directory).rglob("*.rpm"))
    expected = {f"{name}-{version}.x86_64.rpm": name for name, version in OPENSUSE_PACKAGES.items()}
    check(
        len(files) == 2 and {p.name for p in files} == set(expected),
        "Downloaded kernel packages differ; inspect pinned vendor versions",
    )
    for path in files:
        check(
            not path.is_symlink()
            and path.is_file()
            and path.resolve().is_relative_to(Path(directory).resolve()),
            "Downloaded kernel package path differs",
        )
        name = expected[path.name]
        check(
            command(
                ["rpm", "-qp", "--qf", "%{NAME}|%{VERSION}-%{RELEASE}|%{ARCH}", str(path)]
            ).strip()
            == f"{name}|{OPENSUSE_PACKAGES[name]}|x86_64",
            "Downloaded kernel package identity differs",
        )
        signature = command(["rpm", "-qp", "--qf", "%{RSAHEADER:pgpsig}", str(path)]).strip()
        check(
            signature.startswith("RSA/") and "," in signature,
            "Vendor kernel package lacks signature; inspect repository",
        )
        command(["rpm", "--checksig", str(path)])
    return sorted(str(path) for path in files)


def verify_opensuse_packages():
    check(platform.release() == OPENSUSE_KERNEL, "Pinned openSUSE running kernel differs")
    names = command(["rpm", "-qa", "--qf", "%{NAME}\n"]).splitlines()
    check("kernel-default-base" not in names, "Base kernel variant remains installed")
    for name, version in OPENSUSE_PACKAGES.items():
        check(
            command(["rpm", "-q", "--qf", "%{EPOCHNUM}:%{VERSION}-%{RELEASE}", name]).strip()
            == "0:" + version,
            "Pinned openSUSE prerequisite package differs",
        )


def prepare_opensuse_kernel():
    check(platform.release() == OPENSUSE_KERNEL, "Pinned openSUSE running kernel differs")
    check(
        command(
            ["rpm", "-q", "--qf", "%{NAME}|%{VERSION}-%{RELEASE}|%{ARCH}", "kernel-default-base"]
        ).strip()
        == f"kernel-default-base|{OPENSUSE_BASE}|x86_64",
        "Fresh openSUSE kernel variant differs; inspect owned partial",
    )
    security_state("opensuse")
    check(
        shutil.disk_usage("/").free >= 512 * 1024**2,
        "Insufficient guest free space for pinned kernel prerequisite",
    )
    kernel = Path("/boot/vmlinuz-" + OPENSUSE_KERNEL)
    before = hashlib.sha256(kernel.read_bytes()).digest()
    with tempfile.TemporaryDirectory(prefix="kdive-kernel-") as directory:
        files = download_kernel_packages(directory)
        confirm_kernel_transaction(
            [
                "zypper",
                "--xmlout",
                "--no-refresh",
                "install",
                "--no-recommends",
                "--",
                *files,
                "-kernel-default-base",
            ]
        )
    verify_opensuse_packages()
    check(
        hashlib.sha256(kernel.read_bytes()).digest() == before,
        "Installed kernel binary changed; inspect preparing guest before reboot",
    )
    security_state("opensuse")


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
    if profile == "opensuse":
        prepare_opensuse_kernel()
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
    if profile == "opensuse":
        check(
            "[integrity]" in Path("/sys/kernel/security/lockdown").read_text().split(),
            "openSUSE kernel lockdown differs; preserve signed module enforcement",
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
    if profile == "opensuse":
        verify_opensuse_packages()
        names += list(OPENSUSE_PACKAGES)
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
        "boot_id": machine_uuid(Path("/proc/sys/kernel/random/boot_id").read_text()),
        "cpus": os.cpu_count(),
        "memory_bytes": int(memory["MemTotal"].split()[0]) * 1024,
        "crash_reserved_bytes": crash_reservation(),
        "balloon_driver": any(Path("/sys/bus/virtio/drivers/virtio_balloon").glob("virtio[0-9]*")),
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
    machine_uuid(result.get("boot_id"))
    host = request["host"]
    check(
        type(result.get("cpus")) is int and result["cpus"] == host["cores"],
        "Guest CPU count differs",
    )
    configured = host["memory_mib"] * 1024**2
    balloon = host.get("balloon_mib", host["memory_mib"])
    check(
        not balloon or result.get("balloon_driver") is True,
        "VirtIO balloon driver is not bound; inspect guest modules/device",
    )
    target = (balloon or host["memory_mib"]) * 1024**2
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
        usable + reserved >= target * 0.9,
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


def reboot(request, previous_boot):
    check(os.geteuid() == 0 and platform.system() == "Linux", "Reboot requires Linux and sudo")
    check(request["host"]["profile"] == "opensuse", "Unexpected guest reboot profile")
    validate_identity(request, identity_observation())
    check(
        machine_uuid(Path("/proc/sys/kernel/random/boot_id").read_text())
        == machine_uuid(previous_boot),
        "Guest boot changed before reboot; inspect partial",
    )
    command(["systemctl", "--no-block", "reboot"], timeout=30)
    return {"reboot_requested": True}


def main():
    try:
        envelope = json.loads(sys.stdin.read(65537))
        check(isinstance(envelope, dict), "Invalid guest request")
        if set(envelope) == {"request", "reboot_from"}:
            result = reboot(envelope["request"], envelope["reboot_from"])
        else:
            check(
                set(envelope) == {"request", "fresh"} and type(envelope["fresh"]) is bool,
                "Invalid guest verification request",
            )
            result = run(envelope["request"], envelope["fresh"])
        print(json.dumps(result, sort_keys=True))
    except (GuestError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            "Guest baseline failed; inspect private cloud-init, identity, sizing, security and KVM",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
