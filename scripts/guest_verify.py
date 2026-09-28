"""Management-only preparation and reusable Linux guest baseline verification."""

import errno
import fcntl
import hashlib
import json
import os
import platform
import pty
import pwd
import re
import select
import shlex
import shutil
import socket
import stat
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
    deadline = time.monotonic() + 30
    while True:
        try:
            value = Path("/sys/kernel/kexec_crash_size").read_text().strip()
            break
        except FileNotFoundError:
            return 0
        except OSError as error:
            if error.errno != errno.EBUSY:
                raise
            # The sysfs read shares the kexec lock with boot-time kdump loading.
            remaining = deadline - time.monotonic()
            check(remaining > 0, "Crash memory reservation read timed out; inspect kdump")
            time.sleep(min(0.25, remaining))
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


# Package lists follow KDIVE docs/operating/install.md; this is only its speed layer.
TOOLCHAIN_PACKAGES = {
    "ubuntu": "build-essential pkg-config libvirt-dev python3-dev libelf-dev shellcheck shfmt "
    "libvirt-daemon-system libvirt-clients qemu-system-x86 docker.io docker-compose-v2",
    "fedora": "gcc make pkgconf-pkg-config libvirt-devel python3-devel elfutils-libelf-devel "
    "ShellCheck shfmt libvirt libvirt-client qemu-kvm moby-engine docker-compose",
    "rocky": "gcc make pkgconf-pkg-config libvirt-devel python3-devel elfutils-libelf-devel "
    "ShellCheck shfmt libvirt libvirt-client qemu-kvm docker-ce docker-ce-cli containerd.io "
    "docker-buildx-plugin docker-compose-plugin",
    "opensuse": "gcc make pkg-config libvirt-devel python3-devel libelf-devel ShellCheck shfmt "
    "libvirt-daemon-qemu libvirt-daemon-proxy libvirt-client qemu-x86 docker docker-compose",
}
UV_VERSION = "0.12.19"
JUST_VERSION = "1.58.0"
DOCKER_KEY = "060A61C51B558A7F742B77AAC52FEB6B621E9F35"
DOCKER_REPOSITORY = (
    "[docker-ce-stable]\nname=Docker CE Stable\n"
    "baseurl=https://download.docker.com/linux/rhel/$releasever/$basearch/stable\n"
    "enabled=1\ngpgcheck=1\nsslverify=1\n"
    "gpgkey=https://download.docker.com/linux/rhel/gpg\n"
)


TOOLCHAIN_DIAGNOSTICS = (
    {
        f"Toolchain operator {tool} failed; inspect guest prerequisites and re-prepare": (
            f"Toolchain {tool} unavailable for operator; restore toolchain or re-prepare"
        )
        for tool in (
            "bash",
            "id",
            "git",
            "curl",
            "gcc",
            "make",
            "pkg-config",
            "python3",
            "shellcheck",
            "shfmt",
            "realpath",
            "find",
            "grep",
            "uv",
            "just",
            "docker",
            "virsh",
            "/usr/bin/qemu-system-x86_64",
            "/usr/bin/qemu-kvm",
            "/usr/libexec/qemu-kvm",
        )
    }
    | {
        f"Toolchain operator missing {group} group; re-prepare": (
            f"Toolchain operator missing {group} group; restore toolchain or re-prepare"
        )
        for group in ("docker", "kvm", "libvirt")
    }
    | {
        "Toolchain required packages failed; inspect guest prerequisites and re-prepare": (
            "Toolchain required packages unavailable; restore toolchain or re-prepare"
        ),
        "Toolchain required packages missing; re-prepare": (
            "Toolchain required packages unavailable; restore toolchain or re-prepare"
        ),
        "Toolchain QEMU missing; re-prepare": (
            "Toolchain QEMU unavailable; restore toolchain or re-prepare"
        ),
    }
)


def toolchain_packages(profile):
    return "bash coreutils findutils grep git curl ca-certificates".split() + (
        TOOLCHAIN_PACKAGES[profile].split()
    )


def toolchain_command(argv, operation, timeout=60):
    try:
        return command(argv, timeout=timeout)
    except GuestError:
        raise GuestError(
            f"Toolchain {operation} failed; inspect guest prerequisites and re-prepare"
        ) from None


def operator_command(user, argv, timeout=60):
    return toolchain_command(
        ["runuser", "--login", user, "--shell", "/bin/bash", "--command", shlex.join(argv)],
        f"operator {argv[0]}",
        timeout,
    )


def toolchain_login_check(bash, groups):
    check(bool(re.fullmatch(r"[0-9]+\.[0-9]+", bash)), "Toolchain Bash version unavailable")
    check(tuple(map(int, bash.split("."))) >= (4, 4), "Toolchain Bash must be at least 4.4")
    for group in ("docker", "kvm", "libvirt"):
        check(group in groups.split(), f"Toolchain operator missing {group} group; re-prepare")


def toolchain_source_path(account, prepare=False):
    home = Path(account.pw_dir)
    check(
        account.pw_uid != 0 and home.is_absolute() and home != Path("/"),
        "Unsafe toolchain operator home/account; select a non-root operator",
    )
    for path in (*reversed(home.parents), home, home / "src"):
        if path == home / "src" and prepare and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o755)
            os.chown(path, account.pw_uid, account.pw_gid)
        info = path.lstat()
        check(stat.S_ISDIR(info.st_mode), "Toolchain path is a symlink or not a directory")
        owned = path in (home, home / "src")
        check(info.st_uid == (account.pw_uid if owned else 0), "Toolchain path ownership differs")
        if owned and prepare:
            path.chmod(stat.S_IMODE(info.st_mode) & ~0o022)
        else:
            check(
                not info.st_mode & 0o022, "Toolchain path is writable by group/others; repair modes"
            )


def toolchain_package_hash(profile):
    argv = (
        ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n"]
        if profile == "ubuntu"
        else ["rpm", "-qa", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n"]
    )
    rows = toolchain_command(argv, "package inventory").splitlines()
    if profile == "ubuntu":
        rows = [row for row in rows if row.endswith("\tinstalled")]
    return hashlib.sha256(("\n".join(sorted(rows)) + "\n").encode()).hexdigest()


def toolchain_check_packages(profile):
    packages = toolchain_packages(profile)
    argv = (
        ["dpkg-query", "-W", "-f=${db:Status-Status}\n", *packages]
        if profile == "ubuntu"
        else ["rpm", "-q", "--quiet", *packages]
    )
    installed = toolchain_command(argv, "required packages")
    if profile == "ubuntu":
        check(
            installed.splitlines() == ["installed"] * len(packages),
            "Toolchain required packages missing; re-prepare",
        )


def check_docker_key(output):
    fingerprints = [row.split(":")[9] for row in output.splitlines() if row.startswith("fpr:")]
    check(fingerprints == [DOCKER_KEY], "Docker signing key differs; inspect approved source")


def prepare_docker_repository():
    path = Path("/etc/yum.repos.d/docker-ce.repo")
    check(
        not path.is_symlink() and (not path.exists() or path.read_text() == DOCKER_REPOSITORY),
        "Docker repository differs; inspect existing source without replacing it",
    )
    toolchain_command(
        ["dnf", "install", "-y", "gnupg2", "curl", "ca-certificates"],
        "repository verification prerequisites",
        600,
    )
    with tempfile.TemporaryDirectory(prefix="kdive-docker-key-") as directory:
        key = str(Path(directory) / "gpg")
        toolchain_command(
            [
                "curl",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--output",
                key,
                "https://download.docker.com/linux/rhel/gpg",
            ],
            "Docker signing key download",
            120,
        )
        check_docker_key(
            toolchain_command(
                ["gpg", "--homedir", directory, "--batch", "--show-keys", "--with-colons", key],
                "Docker signing key",
            )
        )
        toolchain_command(["rpm", "--import", key], "Docker signing key import")
    path.write_text(DOCKER_REPOSITORY)
    path.chmod(0o644)


def prepare_operator_tools(user, account):
    home = Path(account.pw_dir)
    profile = next(
        (
            home / name
            for name in (".bash_profile", ".bash_login", ".profile")
            if (home / name).exists() or (home / name).is_symlink()
        ),
        home / ".profile",
    )
    if profile.exists() or profile.is_symlink():
        info = profile.lstat()
        check(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == account.pw_uid
            and not info.st_mode & 0o022,
            "Unsafe operator login profile; repair ownership/mode",
        )
    line = '\nexport PATH="$HOME/.local/bin:$PATH"\n'
    if not profile.exists() or line.strip() not in profile.read_text():
        with profile.open("a") as stream:
            stream.write(line)
    os.chown(profile, account.pw_uid, account.pw_gid)
    profile.chmod(0o644)
    with tempfile.TemporaryDirectory(prefix="kdive-uv-") as directory:
        installer = Path(directory) / "install.sh"
        toolchain_command(
            [
                "curl",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--output",
                str(installer),
                f"https://astral.sh/uv/{UV_VERSION}/install.sh",
            ],
            "uv installer download",
            120,
        )
        Path(directory).chmod(0o755)
        installer.chmod(0o644)
        operator_command(
            user,
            [
                "env",
                "UV_NO_MODIFY_PATH=1",
                f"UV_INSTALL_DIR={home}/.local/bin",
                "sh",
                str(installer),
            ],
            300,
        )
    operator_command(user, ["uv", "tool", "install", f"rust-just=={JUST_VERSION}"], 300)


def toolchain_observation(request):
    profile, user = request["host"]["profile"], request["host"]["ansible_user"]
    toolchain_source_path(pwd.getpwnam(user))
    toolchain_check_packages(profile)
    bash = operator_command(
        user, ["bash", "-c", 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"']
    )
    toolchain_login_check(bash, operator_command(user, ["id", "-Gn"]))
    for tool in ("git", "curl", "gcc", "make", "pkg-config", "python3", "shellcheck", "shfmt"):
        operator_command(user, [tool, "--version"])
    for tool in ("realpath", "find", "grep"):
        check("GNU" in operator_command(user, [tool, "--version"]), f"Toolchain {tool} must be GNU")
    qemu = shutil.which("qemu-system-x86_64") or shutil.which("qemu-kvm")
    if qemu is None and Path("/usr/libexec/qemu-kvm").is_file():
        qemu = "/usr/libexec/qemu-kvm"
    check(qemu is not None, "Toolchain QEMU missing; re-prepare")
    operator_command(user, [qemu, "--version"])
    operator_command(user, ["docker", "compose", "version"])
    operator_command(user, ["docker", "info"])
    operator_command(user, ["virsh", "-c", "qemu:///system", "version"])
    toolchain_command(["systemctl", "is-enabled", "docker.service"], "Docker enablement")
    toolchain_command(["systemctl", "is-active", "docker.service"], "Docker service")
    versions = {}
    for name, argv in (
        ("uv", ["uv", "--version"]),
        ("just", ["just", "--version"]),
        ("docker", ["docker", "version", "--format", "{{.Server.Version}}"]),
        ("libvirt", ["virsh", "--version"]),
    ):
        value = operator_command(user, argv).strip()
        if name in {"uv", "just"}:
            parts = value.split()
            check(len(parts) >= 2 and parts[0] == name, f"Toolchain {name} version invalid")
            value = parts[1]
        check(
            bool(re.fullmatch(r"[A-Za-z0-9.+:~_%-]{1,180}", value)),
            f"Toolchain {name} version invalid; inspect installed tool",
        )
        versions[name] = value
    return {
        "distro": profile,
        "release": request["template"]["image"]["release"],
        **versions,
        "packages_sha256": toolchain_package_hash(profile),
    }


def prepare_toolchain(request):
    profile, user = request["host"]["profile"], request["host"]["ansible_user"]
    account = pwd.getpwnam(user)
    toolchain_source_path(account, prepare=True)
    if profile == "rocky":
        prepare_docker_repository()
    packages = toolchain_packages(profile)
    if profile == "ubuntu":
        toolchain_command(["apt-get", "update"], "package indexes", 600)
        argv = [
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *packages,
        ]
    elif profile == "opensuse":
        argv = ["zypper", "--non-interactive", "install", "--no-recommends", *packages]
    else:
        argv = ["dnf", "install", "-y", *packages]
    toolchain_command(argv, f"{profile} package installation", 900)
    for group in ("docker", "kvm", "libvirt"):
        toolchain_command(["groupadd", "--force", "--system", group], f"{group} group")
    toolchain_command(
        ["usermod", "--append", "--groups", "docker,kvm,libvirt", user], "operator groups"
    )
    units = toolchain_command(
        ["systemctl", "list-unit-files", "virtqemud.socket", "libvirtd.service", "--no-legend"],
        "libvirt units",
    )
    libvirt = (
        [
            "virtqemud.socket",
            "virtnetworkd.socket",
            "virtstoraged.socket",
            "virtnodedevd.socket",
            "virtsecretd.socket",
            "virtproxyd.socket",
        ]
        if "virtqemud.socket" in units
        else ["libvirtd.service"]
    )
    toolchain_command(
        ["systemctl", "enable", "--now", "docker.service", *libvirt], "runtime services", 120
    )
    prepare_operator_tools(user, account)
    content = toolchain_observation(request)
    check(
        content["uv"] == UV_VERSION and content["just"] == JUST_VERSION,
        "Toolchain pinned uv/just differs; re-prepare",
    )
    return content


def check_toolchain(request, content):
    fields = {"distro", "release", "uv", "just", "docker", "libvirt", "packages_sha256"}
    check(
        isinstance(content, dict)
        and set(content) == fields
        and isinstance(content["packages_sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", content["packages_sha256"]),
        "Invalid toolchain content",
    )
    observed = toolchain_observation(request)
    for key in fields - {"packages_sha256"}:
        check(content[key] == observed[key], f"Toolchain {key} differs; restore or re-prepare")


LEVELS = (
    {
        "name": "toolchain",
        "parent": "clean",
        "prepare": prepare_toolchain,
        "check": check_toolchain,
    },
)
LEVEL_DIRECTORY = Path("/var/lib/kdive-levels")


def level_chain(level):
    chain, parent = [], "clean"
    for item in LEVELS:
        check(
            isinstance(item, dict)
            and set(item) == {"name", "parent", "prepare", "check"}
            and isinstance(item["name"], str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", item["name"])
            and item["name"] not in {"clean", "current", *(i["name"] for i in chain)}
            and item["parent"] == parent
            and callable(item["prepare"])
            and callable(item["check"]),
            "Invalid closed level registry; repair installed code",
        )
        chain.append(item)
        parent = item["name"]
    if level == "clean":
        return []
    for index, item in enumerate(chain):
        if item["name"] == level:
            return chain[: index + 1]
    raise GuestError("Unknown level; select a level implemented by this checkout")


def level_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            check(key not in result, "Duplicate level metadata field; inspect manifest")
            result[key] = item
        return result

    def invalid(value):
        raise GuestError("Non-finite level metadata; inspect manifest")

    check(isinstance(value, str) and len(value.encode()) <= 65536, "Level metadata exceeds bound")
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, RecursionError):
        raise GuestError("Invalid level metadata; inspect manifest") from None


def level_equal(left, right):
    # Python equality conflates JSON booleans, integers and floats.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def level_metadata(metadata, name, parent, identity, config_sha256):
    item = level_chain(name)[-1]
    check(
        isinstance(metadata, dict)
        and set(metadata)
        == {"schema", "level", "parent", "parent_identity", "identity", "config_sha256", "content"}
        and type(metadata["schema"]) is int
        and metadata["schema"] == 1
        and metadata["level"] == name
        and metadata["parent"] == item["parent"]
        and metadata["identity"] == identity
        and metadata["config_sha256"] == config_sha256
        and isinstance(metadata["content"], dict),
        "Level metadata identity/configuration differs; re-prepare selected level",
    )
    expected = hashlib.sha256(
        json.dumps(parent, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    check(
        metadata["parent_identity"] == expected,
        "Level parent identity differs; re-prepare selected level",
    )
    level_json(json.dumps(metadata, allow_nan=False))


def level_manifest(name, metadata, write=False):
    check(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", name),
        "Invalid manifest name",
    )
    if write:
        LEVEL_DIRECTORY.mkdir(mode=0o755, exist_ok=True)
    directory = os.open(LEVEL_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        check(info.st_uid == 0 and not info.st_mode & 0o022, "Unsafe level manifest directory")
        filename = name + ".json"
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            check(write, "Level manifest missing; restore or re-prepare selected level")
        else:
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                check(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == 0
                    and stat.S_IMODE(info.st_mode) == 0o644,
                    "Unsafe level manifest file",
                )
                check(
                    level_equal(level_json(stream.read(65537)), metadata),
                    "Guest and snapshot level metadata differ; restore selected level",
                )
            return
        payload = json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False)
        level_json(payload)
        fd, temporary = tempfile.mkstemp(prefix=".level-", dir=LEVEL_DIRECTORY)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, filename, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        os.close(directory)


def run_level(request, operation, name, chain, proposed):
    check(operation in {"verify", "prepare"}, "Invalid level operation")
    entries = level_chain(name)
    check(entries and isinstance(chain, list), "Invalid level chain")
    count = len(entries) if operation == "prepare" else len(entries) + 1
    check(len(chain) == count and isinstance(chain[0], dict), "Incomplete level chain")
    parent = chain[0]
    check(
        set(parent) == {"schema", "identity", "config_sha256"}
        and type(parent["schema"]) is int
        and parent["schema"] == 1,
        "Invalid clean level metadata",
    )
    for field in ("identity", "config_sha256"):
        check(
            isinstance(parent[field], str) and re.fullmatch(r"[a-f0-9]{64}", parent[field]),
            "Invalid clean metadata digest",
        )
    for item, metadata in zip(entries, chain[1:], strict=False):
        level_metadata(
            metadata, item["name"], parent, chain[0]["identity"], chain[0]["config_sha256"]
        )
        parent = metadata
    if operation == "prepare":
        level_metadata(proposed, name, parent, chain[0]["identity"], chain[0]["config_sha256"])
        check(proposed["content"] == {}, "Unexpected proposed level content")
    else:
        check(proposed is None, "Unexpected verification metadata")
    run(request, fresh=False)
    for item, metadata in zip(entries, chain[1:], strict=False):
        level_manifest(item["name"], metadata)
        item["check"](request, metadata["content"])
    if operation == "verify":
        return {"verified": True}
    metadata = dict(proposed, content=entries[-1]["prepare"](request))
    level_metadata(metadata, name, parent, chain[0]["identity"], chain[0]["config_sha256"])
    level_manifest(name, metadata, write=True)
    entries[-1]["check"](request, metadata["content"])
    level_manifest(name, metadata)
    # Preparation must not invalidate the parent checks or baseline readiness.
    run(request, fresh=False)
    for item, ancestor in zip(entries, chain[1:], strict=False):
        level_manifest(item["name"], ancestor)
        item["check"](request, ancestor["content"])
    return {"metadata": metadata}


def main():
    envelope = {}
    try:
        envelope = json.loads(sys.stdin.read(65537))
        check(isinstance(envelope, dict), "Invalid guest request")
        if set(envelope) == {"request", "level_operation", "level", "chain", "proposed"}:
            result = run_level(
                envelope["request"],
                envelope["level_operation"],
                envelope["level"],
                envelope["chain"],
                envelope["proposed"],
            )
        elif set(envelope) == {"request", "reboot_from"}:
            result = reboot(envelope["request"], envelope["reboot_from"])
        else:
            check(
                set(envelope) == {"request", "fresh"} and type(envelope["fresh"]) is bool,
                "Invalid guest verification request",
            )
            result = run(envelope["request"], envelope["fresh"])
        print(json.dumps(result, sort_keys=True))
    except (GuestError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        diagnostic = None
        if (
            isinstance(error, GuestError)
            and isinstance(envelope, dict)
            and set(envelope) == {"request", "level_operation", "level", "chain", "proposed"}
        ):
            diagnostic = TOOLCHAIN_DIAGNOSTICS.get(str(error))
        print(
            diagnostic
            or (
                "Guest baseline failed; inspect private cloud-init, identity, "
                "sizing, security and KVM"
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
