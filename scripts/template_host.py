"""Native, non-destructive template lifecycle; transported over authenticated SSH."""

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE = Path("/var/cache/kdive-templates")
LOCKS = Path("/run/lock/kdive-templates")
DOWNLOAD_TIMEOUT = 900
FIXED = {
    "cores": "2",
    "memory": "2048",
    "cpu": "host",
    "bios": "ovmf",
    "scsihw": "virtio-scsi-pci",
    "serial0": "socket",
    "vga": "serial0",
    "boot": "order=scsi0",
    "ostype": "l26",
    "citype": "nocloud",
    "onboot": "0",
    "agent": "0",
}


class TemplateError(ValueError):
    """Public-safe failure; never include supplied values or command output."""


def check(condition, message):
    if not condition:
        raise TemplateError(message)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def identity(request):
    return digest(
        {key: value for key, value in request.items() if key not in {"apply", "resume"}}
        | {"configuration": FIXED, "schema": 1}
    )


def command(argv, timeout=120):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise TemplateError(
            "Native command unavailable or timed out; inspect host task state"
        ) from None
    check(result.returncode == 0, "Native command failed; inspect host task state before retry")
    return result.stdout


def native(path, *options):
    try:
        return json.loads(command(["pvesh", "get", path, *options, "--output-format", "json"]))
    except json.JSONDecodeError:
        raise TemplateError("Native API returned invalid JSON; inspect host service") from None


def validate_request(request):
    check(isinstance(request, dict), "Invalid template request")
    expected = {
        "profile",
        "template_vmid",
        "node",
        "storage",
        "bridge",
        "vlan",
        "cpu",
        "image",
        "apply",
        "resume",
    }
    check(set(request) == expected, "Unexpected template request fields")
    for key in ("node", "storage", "bridge"):
        check(
            isinstance(request[key], str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", request[key]),
            "Invalid native identifier",
        )
    check(request["profile"] in {"ubuntu", "fedora", "rocky", "opensuse"}, "Invalid image profile")
    check(
        type(request["template_vmid"]) is int and 100 <= request["template_vmid"] <= 999999999,
        "Invalid template ID",
    )
    vlan = request["vlan"]
    check(vlan is None or type(vlan) is int and 1 <= vlan <= 4094, "Invalid VLAN tag")
    check(request["cpu"] == "host", "CPU must be host")
    check(all(type(request[key]) is bool for key in ("apply", "resume")), "Invalid action flags")
    check(not request["resume"] or request["apply"], "Resume requires apply")
    image = request["image"]
    check(isinstance(image, dict), "Invalid image profile record")
    check(
        isinstance(image.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", image["sha256"]),
        "Invalid image digest",
    )
    check(
        isinstance(image.get("url"), str) and image["url"].startswith("https://"),
        "Image requires HTTPS",
    )
    check(
        all(
            type(image.get(key)) is int and image[key] > 0
            for key in ("size_bytes", "virtual_size_bytes")
        ),
        "Invalid image size",
    )
    check(image.get("bios") == FIXED["bios"], "Unsupported image firmware")


def private_directory(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    check(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "Unsafe private directory; inspect ownership/mode",
    )


@contextlib.contextmanager
def template_lock(vmid):
    private_directory(LOCKS)
    fd = os.open(LOCKS / f"{vmid}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        check(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077,
            "Unsafe template lock file",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemplateError("Template busy; wait for the owning operation") from None
        yield
    finally:
        os.close(fd)


def inspect_image(path, image):
    info = path.lstat()
    check(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o077,
        "Unsafe cached image file",
    )
    check(info.st_size == image["size_bytes"], "Image byte length differs from pin")
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    check(checksum == image["sha256"], "Image SHA256 differs from pin")
    try:
        metadata = json.loads(command(["qemu-img", "info", "--output=json", str(path)]))
    except json.JSONDecodeError:
        raise TemplateError("Image metadata is invalid") from None
    check(
        isinstance(metadata, dict)
        and metadata.get("format") == "qcow2"
        and metadata.get("virtual-size") == image["virtual_size_bytes"],
        "Image format/size differs from pin",
    )
    data = metadata.get("format-specific", {}).get("data", {})
    check(
        not any(
            metadata.get(key)
            for key in ("backing-filename", "full-backing-filename", "encrypted", "dirty-flag")
        )
        and not any(data.get(key) for key in ("data-file", "encrypt", "corrupt")),
        "Image has unsafe backing, encryption, external data or dirty metadata",
    )


def cached_image(image):
    def deadline_expired(signum, frame):
        raise TemplateError("Image download exceeded deadline; retry vendor access")

    private_directory(CACHE)
    destination = CACHE / (image["sha256"] + ".qcow2")
    if destination.exists() or destination.is_symlink():
        inspect_image(destination, image)
        return destination
    fd, temporary = tempfile.mkstemp(dir=CACHE, prefix="download-")
    path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            previous_handler = signal.signal(signal.SIGALRM, deadline_expired)
            try:
                # A socket timeout alone can be extended indefinitely by a slow sender.
                signal.setitimer(signal.ITIMER_REAL, DOWNLOAD_TIMEOUT)
                with urllib.request.urlopen(image["url"], timeout=30) as source:
                    check(source.geturl().startswith("https://"), "Image redirect requires HTTPS")
                    count = 0
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        check(count <= image["size_bytes"], "Image download exceeded pinned size")
                        output.write(chunk)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
            output.flush()
            os.fsync(output.fileno())
        inspect_image(path, image)
        # Concurrent different-template downloads may publish the same verified digest.
        os.replace(path, destination)
        return destination
    except (OSError, urllib.error.URLError):
        raise TemplateError(
            "Image download failed; check vendor access and cache capacity"
        ) from None
    finally:
        path.unlink(missing_ok=True)


def host_admission(request):
    check(
        os.geteuid() == 0 and platform.system() == "Linux" and platform.machine() == "x86_64",
        "Native host requires root on x86_64 Linux",
    )
    check(
        all(shutil.which(tool) for tool in ("qm", "pvesh", "qemu-img")),
        "Native host requires qm, pvesh and qemu-img",
    )
    nodes = native("/cluster/status")
    check(
        isinstance(nodes, list) and all(isinstance(row, dict) for row in nodes),
        "Invalid native cluster identity",
    )
    local = [
        row.get("name") for row in nodes if row.get("type") == "node" and row.get("local") == 1
    ]
    check(
        local == [request["node"]],
        "Native local node differs from admitted node; fix SSH destination",
    )
    path = f"/nodes/{request['node']}"
    storage = native(f"{path}/storage/{request['storage']}/status")
    check(
        isinstance(storage, dict)
        and storage.get("active") == 1
        and storage.get("enabled") == 1
        and storage.get("type") in {"zfspool", "lvmthin"}
        and "images" in str(storage.get("content", "")).split(","),
        "Storage must be active image-capable zfspool or lvmthin",
    )
    bridges = native(f"{path}/network")
    check(
        isinstance(bridges, list) and all(isinstance(row, dict) for row in bridges),
        "Invalid native network inventory",
    )
    bridge = [row for row in bridges if row.get("iface") == request["bridge"]]
    check(
        len(bridge) == 1 and bridge[0].get("active") == 1 and bridge[0].get("type") == "bridge",
        "Configured bridge is not active",
    )
    check(
        request["vlan"] is None
        or bridge[0].get("bridge_vlan_aware") == 1
        or bool(bridge[0].get("bridge_ports")),
        "Tagged network requires bridge VLAN support/uplink",
    )
    return storage


def existing_resource(request):
    resources = native("/cluster/resources", "--type", "vm")
    check(
        isinstance(resources, list)
        and all(
            isinstance(row, dict)
            and type(row.get("vmid")) is int
            and row.get("type") in {"qemu", "lxc"}
            and isinstance(row.get("node"), str)
            for row in resources
        ),
        "Invalid native VM inventory",
    )
    matches = [row for row in resources if row["vmid"] == request["template_vmid"]]
    check(len(matches) <= 1, "Ambiguous native VM inventory")
    if matches:
        check(
            matches[0]["node"] == request["node"] and matches[0]["type"] == "qemu",
            "Template ID belongs to another node or resource; select a new ID",
        )
    return bool(matches)


def properties(value):
    check(isinstance(value, str), "Invalid native device configuration")
    result = {}
    for field in value.split(","):
        key, separator, item = field.partition("=")
        check(key not in result, "Duplicate native device property")
        result[key] = item if separator else None
    return result


def verify_configuration(request, marker):
    base = f"/nodes/{request['node']}/qemu/{request['template_vmid']}"
    config = native(base + "/config")
    status = native(base + "/status/current")
    pending = native(base + "/pending")
    check(
        isinstance(config, dict)
        and isinstance(status, dict)
        and isinstance(pending, list)
        and all(isinstance(row, dict) for row in pending),
        "Invalid native configuration response",
    )
    check(
        status.get("status") == "stopped"
        and "lock" not in config
        and not any("pending" in row or row.get("delete") for row in pending),
        "Template is running, locked or has pending changes; inspect before retry",
    )
    check(config.get("description") == marker, "Template identity/phase differs; select a new ID")
    expected = FIXED | {"name": f"kdive-{request['profile']}-{request['template_vmid']}"}
    allowed = set(expected) | {
        "description",
        "net0",
        "scsi0",
        "ide2",
        "efidisk0",
        "template",
        "digest",
        "meta",
        "smbios1",
        "vmgenid",
    }
    check(
        set(config) <= allowed
        and all(str(config.get(key)) == value for key, value in expected.items()),
        "Template configuration differs; select a new ID",
    )
    net = properties(config.get("net0"))
    expected_net = {"virtio", "bridge"} | ({"tag"} if request["vlan"] is not None else set())
    check(
        set(net) == expected_net
        and net.get("bridge") == request["bridge"]
        and re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", net.get("virtio") or "")
        and (request["vlan"] is None or net.get("tag") == str(request["vlan"])),
        "Template network differs; select a new ID",
    )
    verify_disks(request, config)
    feature = "clone" if config.get("template") == 1 else "snapshot"
    capability = native(base + "/feature", "--feature", feature)
    check(
        isinstance(capability, dict) and capability.get("hasFeature") == 1,
        "Native disks lack required snapshot/clone capability",
    )
    return config


def verify_disks(request, config):
    volumes = native(f"/nodes/{request['node']}/storage/{request['storage']}/content")
    check(
        isinstance(volumes, list) and all(isinstance(row, dict) for row in volumes),
        "Invalid native volume inventory",
    )
    owned = [row for row in volumes if row.get("vmid") == request["template_vmid"]]
    expected_ids = set()
    for slot in ("scsi0", "ide2", "efidisk0"):
        parts = properties(config.get(slot))
        ids = [key for key, value in parts.items() if value is None]
        check(
            len(ids) == 1 and ids[0].startswith(request["storage"] + ":"),
            "Template disk uses unexpected storage/reference",
        )
        volume_id = ids[0]
        expected_ids.add(volume_id)
        options = {key: value for key, value in parts.items() if key != volume_id}
        allowed = {
            "scsi0": {"size"},
            "ide2": {"media", "size"},
            "efidisk0": {"efitype", "pre-enrolled-keys", "ms-cert", "size"},
        }[slot]
        check(set(options) <= allowed, "Template disk options differ")
        if slot == "ide2":
            check(
                options.get("media") == "cdrom" and volume_id.endswith("cloudinit"),
                "Template cloud-init disk differs",
            )
        if slot == "efidisk0":
            # Proxmox adds this informational marker after enrolling its firmware keys.
            check(
                options.get("ms-cert") in {None, "2011", "2023", "2023w", "2023k"},
                "Unknown native EFI certificate marker",
            )
            check(
                options.get("efitype") == "4m" and options.get("pre-enrolled-keys") == "1",
                "Template EFI security configuration differs",
            )
        matches = [row for row in owned if row.get("volid") == volume_id]
        check(
            len(matches) == 1
            and matches[0].get("format") == "raw"
            and matches[0].get("content") == "images",
            "Template disk ownership/format differs",
        )
        size = matches[0].get("size")
        minimum = request["image"]["virtual_size_bytes"] if slot == "scsi0" else 1
        maximum = minimum + 1024**2 if slot == "scsi0" else 8 * 1024**2
        check(
            type(size) is int and minimum <= size <= maximum,
            "Template disk allocation size differs",
        )
    check(
        len(expected_ids) == 3
        and {row.get("volid") for row in owned} == expected_ids
        and len(owned) == 3,
        "Unexpected or missing owned disks; inspect residual allocations",
    )


def create(request, marker, image):
    options = FIXED | {
        "name": f"kdive-{request['profile']}-{request['template_vmid']}",
        "description": marker,
        "net0": f"virtio,bridge={request['bridge']}"
        + (f",tag={request['vlan']}" if request["vlan"] is not None else ""),
        "scsi0": f"{request['storage']}:0,import-from={image}",
        "ide2": f"{request['storage']}:cloudinit",
        "efidisk0": f"{request['storage']}:0,efitype=4m,pre-enrolled-keys=1",
    }
    argv = ["qm", "create", str(request["template_vmid"])]
    for key, value in options.items():
        argv.extend(["--" + key, value])
    command(argv, timeout=1800)


def lifecycle(request, storage, template_identity):
    marker = "kdive-template-v1:" + template_identity + ":"
    exists = existing_resource(request)
    if exists:
        config = native(f"/nodes/{request['node']}/qemu/{request['template_vmid']}/config")
        check(isinstance(config, dict), "Invalid existing template configuration")
        if config.get("description") == marker + "ready":
            config = verify_configuration(request, marker + "ready")
            check(config.get("template") == 1, "Ready object is not a template; inspect state")
            return "preserved", config
        check(
            request["resume"] and config.get("description") == marker + "creating",
            "ID occupied or interrupted; inspect ownership before explicit resume",
        )
        config = verify_configuration(request, marker + "creating")
    else:
        check(not request["resume"], "Resume requires an existing owned template")
        available = storage.get("avail")
        check(
            type(available) is int
            and available >= request["image"]["virtual_size_bytes"] + 16 * 1024**2,
            "Insufficient reported storage space for template disks",
        )
        if not request["apply"]:
            return "would-create", {}
        image = cached_image(request["image"])
        # Recheck after download; qm also enforces cluster-wide allocation exclusivity.
        check(not existing_resource(request), "Template ID became occupied; select a new ID")
        create(request, marker + "creating", image)
        config = verify_configuration(request, marker + "creating")
    if config.get("template") != 1:
        command(["qm", "template", str(request["template_vmid"])], timeout=600)
    verify_configuration(request, marker + "creating")
    command(["qm", "set", str(request["template_vmid"]), "--description", marker + "ready"])
    config = verify_configuration(request, marker + "ready")
    check(config.get("template") == 1, "Template conversion did not persist")
    return "resumed" if exists else "created", config


def observed_phase(request, template_identity):
    try:
        if not existing_resource(request):
            return "absent"
        config = native(f"/nodes/{request['node']}/qemu/{request['template_vmid']}/config")
        marker = "kdive-template-v1:" + template_identity + ":"
        for phase in ("creating", "ready"):
            if isinstance(config, dict) and config.get("description") == marker + phase:
                return phase
        return "unowned"
    except (TemplateError, OSError, ValueError, TypeError, KeyError):
        return "unknown"


def run(request):
    started = time.monotonic()
    validate_request(request)
    storage = host_admission(request)
    template_identity = identity(request)
    lock = template_lock(request["template_vmid"]) if request["apply"] else contextlib.nullcontext()
    with lock:
        try:
            if request["apply"]:
                storage = host_admission(request)
            action, config = lifecycle(request, storage, template_identity)
        except TemplateError as error:
            error.phase = observed_phase(request, template_identity)
            raise
    return {
        "profile": request["profile"],
        "template_vmid": request["template_vmid"],
        "identity": template_identity,
        "action": action,
        "config_sha256": digest(config),
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def main():
    try:
        request = json.loads(sys.stdin.read(65537))
        print(json.dumps(run(request), sort_keys=True))
    except TemplateError as error:
        print(
            json.dumps(
                {"error": "template-operation-failed", "phase": getattr(error, "phase", "unknown")}
            )
        )
        print(
            f"Template operation failed: {error}. No wrapper cleanup was attempted.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print(
            "Template operation failed; inspect private host state. No cleanup attempted.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
