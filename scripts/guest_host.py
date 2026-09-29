"""Owned native guest lifecycle, held through controller-confirmed readiness."""

import contextlib
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import select
import stat
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

if __package__:
    from . import guest_verify, template_host
    from .validate_inventory import NIC_MODELS, validate_host
else:
    import guest_verify
    import template_host
    from validate_inventory import NIC_MODELS, validate_host

GuestError = template_host.TemplateError
check = template_host.check
native = template_host.native
command = template_host.command


class ReasonError(GuestError):
    """A refusal whose code the controller maps to its own operator text (ADR 0015)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def refuse(condition, code, message):
    if not condition:
        raise ReasonError(code, message)


PVE_NODES = Path("/etc/pve/nodes")
BASELINE_FIELDS = (
    "vmid",
    "fqdn",
    "cores",
    "memory_mib",
    "disk_gib",
    "ansible_user",
    "ipv4_cidr",
    "gateway",
    "dns_servers",
    "ssh_public_keys",
)


def identity(request):
    h, t = request["host"], request["template"]
    overrides = {key: h[key] for key in ("cpu", "bridge", "storage") if h[key] != t[key]}
    if h.get("nic_model", "virtio") != "virtio":
        overrides["nic_model"] = h["nic_model"]
    if "nic_queues" in h:
        overrides["nic_queues"] = h["nic_queues"]
    if "cloudinit_snippet_storage" in h:
        overrides["cloudinit_snippet_storage"] = h["cloudinit_snippet_storage"]
    balloon = h.get("balloon_mib", h["memory_mib"])
    if balloon:
        overrides["balloon_mib"] = balloon
    return template_host.digest(
        {
            "schema": 1,
            "management": 1,
            "template": template_host.identity(request["template"]),
            "guest": {key: request["host"][key] for key in BASELINE_FIELDS}
            | {"vlan": request["host"].get("vlan")}
            | overrides,
        }
    )


def marker(request, phase):
    return f"kdive-guest-v1:{identity(request)}:{phase}"


def validate_request(request):
    check(
        isinstance(request, dict)
        and set(request)
        in (
            {"host", "template", "revision"},
            {"host", "template", "revision", "kernel_source"},
            {"host", "template", "revision", "kdive_source"},
        ),
        "Invalid guest request",
    )
    if "kdive_source" in request:
        guest_verify.kdive_inputs(request["kdive_source"])
    if "kernel_source" in request:
        guest_verify.kernel_inputs(request["kernel_source"])
    validate_host(request["host"])
    template_host.validate_request(request["template"])
    h, t = request["host"], request["template"]
    check(not t["apply"] and not t["resume"], "Guest operation cannot mutate a template")
    check(
        all(
            h[a] == t[b]
            for a, b in (
                ("profile", "profile"),
                ("template_vmid", "template_vmid"),
                ("proxmox_node", "node"),
            )
        ),
        "Template and guest inputs differ",
    )
    check(h["vmid"] != h["template_vmid"], "Guest and template IDs collide")
    check(
        h["disk_gib"] * 1024**3 >= t["image"]["virtual_size_bytes"],
        "Guest disk smaller than source image; increase disk_gib",
    )
    check(
        isinstance(request["revision"], str)
        and re.fullmatch(r"[a-f0-9]{40,64}", request["revision"]),
        "Missing provisioning revision",
    )


def check_capacity(requests, node, memory, storages, source_extents=None):
    if not requests:
        return []
    cpu = node.get("cpu")
    cpus = node.get("cpuinfo", {}).get("cpus")
    loads = node.get("loadavg")
    check(
        type(cpu) in {int, float}
        and math.isfinite(cpu)
        and 0 <= cpu <= 1
        and type(cpus) is int
        and cpus > 0
        and isinstance(loads, list)
        and loads,
        "Missing CPU capacity metrics; inspect native node status",
    )
    try:
        load = float(loads[0])
    except (ValueError, TypeError):
        raise GuestError("Invalid CPU load metric; inspect native node status") from None
    check(math.isfinite(load) and load >= 0, "Invalid CPU load metric")
    warnings = []
    cores = sum(r["host"]["cores"] for r in requests)
    free = cpus - math.ceil(max(cpu * cpus, load))
    if cores > free:
        warnings.append({"resource": "cpu", "requested": cores, "available": max(free, 0)})
    match = re.search(r"^MemAvailable:\s+([0-9]+) kB$", memory, re.MULTILINE)
    check(match is not None, "Missing MemAvailable; inspect native memory metrics")
    memory_mib = sum(r["host"]["memory_mib"] for r in requests)
    available_kib = int(match[1])
    if memory_mib * 1024 > available_kib:
        warnings.append(
            {"resource": "memory", "requested": memory_mib, "available": available_kib // 1024}
        )
    for storage, status in storages.items():
        extent = status["extent_bytes"]
        demand = sum(
            template_host.allocated_size(r["host"]["disk_gib"] * 1024**3, extent)
            + 2
            * template_host.allocated_size(
                template_host.allocated_size(
                    8 * 1024**2, (source_extents or {}).get(r["host"]["template_vmid"], 0)
                ),
                extent,
            )
            for r in requests
            if r["host"]["storage"] == storage
        )
        check(type(status.get("avail")) is int, "Invalid native storage status; inspect storage")
        refuse(
            status["avail"] >= demand,
            "storage-insufficient",
            "Insufficient reported storage space; release storage before retry",
        )
    return warnings


def resource_present(request, resources):
    h = request["host"]
    check(
        isinstance(resources, list)
        and all(
            isinstance(r, dict)
            and type(r.get("vmid")) is int
            and r.get("type") in {"qemu", "lxc"}
            and isinstance(r.get("node"), str)
            for r in resources
        ),
        "Invalid native VM inventory; absence not established",
    )
    matches = [r for r in resources if r["vmid"] == h["vmid"]]
    check(len(matches) <= 1, "Ambiguous native guest ID")
    if matches:
        refuse(
            matches[0]["node"] == h["proxmox_node"] and matches[0]["type"] == "qemu",
            "vmid-in-use",
            "Guest ID belongs to another resource; select an unused ID",
        )
    return bool(matches)


def base_path(request):
    h = request["host"]
    return f"/nodes/{h['proxmox_node']}/qemu/{h['vmid']}"


def desired_config(request):
    h = request["host"]
    return {
        "cpu": h["cpu"],
        "bios": "ovmf",
        "scsihw": "virtio-scsi-pci",
        "serial0": "socket",
        "vga": "serial0",
        "boot": "order=scsi0",
        "ostype": "l26",
        "citype": "nocloud",
        "onboot": "0",
        "name": h["fqdn"].split(".")[0],
        "searchdomain": h["fqdn"].split(".", 1)[1],
        "cores": str(h["cores"]),
        "memory": str(h["memory_mib"]),
        "balloon": str(h.get("balloon_mib", h["memory_mib"])),
        "shares": "1000",
        "agent": "1",
        "ciupgrade": "0",
        "ciuser": h["ansible_user"],
        "nameserver": " ".join(h["dns_servers"]),
        "ipconfig0": f"ip={h['ipv4_cidr']},gw={h['gateway']}",
        "sshkeys": "\n".join(h["ssh_public_keys"]) + "\n",
    }


def cloned_mac(config):
    net = template_host.properties(config.get("net0"))
    models = set(net) & NIC_MODELS
    check(len(models) == 1, "Missing cloned NIC model; inspect retained guest")
    mac = net[models.pop()]
    check(
        isinstance(mac, str) and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac),
        "Missing cloned NIC identity; inspect retained guest",
    )
    return mac


def desired_network(request, config):
    h = request["host"]
    mac = cloned_mac(config)
    value = f"{h.get('nic_model', 'virtio')}={mac},bridge={h['bridge']}"
    if "vlan" in h:
        value += f",tag={h['vlan']}"
    if "nic_queues" in h:
        value += f",queues={h['nic_queues']}"
    return value


def network_seed(request, mac):
    """Network-only cloud-init v2 document matched by MAC, never renaming the interface."""
    h = request["host"]
    lines = [
        f"# kdive-guest-network-v1 identity={identity(request)}",
        "version: 2",
        "ethernets:",
        "  kdive0:",
        "    match:",
        f'      macaddress: "{mac.lower()}"',
        "    dhcp4: false",
        f'    addresses: ["{ipaddress.IPv4Interface(h["ipv4_cidr"])}"]',
        "    routes:",
        "      - to: default",
        f'        via: "{h["gateway"]}"',
        "    nameservers:",
        "      addresses: [" + ", ".join(f'"{a}"' for a in h["dns_servers"]) + "]",
        f'      search: ["{h["fqdn"].split(".", 1)[1]}"]',
    ]
    return ("\n".join(lines) + "\n").encode()


def seed_volume(request, config):
    h = request["host"]
    content = network_seed(request, cloned_mac(config))
    name = f"kdive-net-{h['vmid']}-{hashlib.sha256(content).hexdigest()}.yaml"
    return f"{h['cloudinit_snippet_storage']}:snippets/{name}", name, content


def seed_path(volume):
    path = Path(command(["pvesm", "path", volume]).strip())
    check(
        path.is_absolute() and path.name == volume.rsplit("/", 1)[1],
        "Invalid snippet storage path; inspect native storage",
    )
    return path


@contextlib.contextmanager
def seed_directory(path):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        check(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
            "Unsafe snippet directory; inspect ownership/mode",
        )
        yield fd
    finally:
        os.close(fd)


def read_seed(fd, name):
    try:
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file, "rb") as stream:
        info = os.fstat(stream.fileno())
        check(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o022,
            "Unsafe network seed file; inspect before retry",
        )
        return stream.read(65537)


def write_seed(request, config):
    volume, name, content = seed_volume(request, config)
    with seed_directory(seed_path(volume)) as fd:
        existing = read_seed(fd, name)
        if existing is None:
            # Publish complete bytes under the final name without ever replacing a file.
            temporary = f".{name}.{secrets.token_hex(8)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            file = os.open(temporary, flags, 0o600, dir_fd=fd)
            try:
                with os.fdopen(file, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                with contextlib.suppress(FileExistsError):
                    os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            finally:
                os.unlink(temporary, dir_fd=fd)
            os.fsync(fd)
            existing = read_seed(fd, name)
        check(
            existing == content, "Network seed differs from derived content; inspect before retry"
        )
    return volume


def verify_seed(request, config):
    volume, name, content = seed_volume(request, config)
    with seed_directory(seed_path(volume)) as fd:
        data = read_seed(fd, name)
    check(data is not None, "Network seed missing; inspect before retry")
    check(data == content, "Network seed differs from derived content; inspect before retry")


def seed_storage_admission(h):
    storage = h["cloudinit_snippet_storage"]
    status = native(f"/nodes/{h['proxmox_node']}/storage/{storage}/status")
    check(
        isinstance(status, dict)
        and status.get("active") == 1
        and status.get("enabled") == 1
        and "snippets" in str(status.get("content", "")).split(","),
        "Snippet storage must be active, enabled and snippet-capable",
    )
    with seed_directory(seed_path(f"{storage}:snippets/kdive-net-probe.yaml")):
        pass


def seed_references(request, name):
    h = request["host"]
    try:
        files = sorted(PVE_NODES.glob("*/qemu-server/*.conf"))
        files += sorted(PVE_NODES.glob("*/lxc/*.conf"))
        check(
            PVE_NODES / h["proxmox_node"] / "qemu-server" / f"{h['template_vmid']}.conf" in files,
            "Guest removed; network seed retained because the reference inventory is incomplete",
        )
        return any(name in path.read_text() for path in files)
    except (OSError, UnicodeError):
        raise GuestError(
            "Guest removed; network seed retained because the reference inventory is unreadable"
        ) from None


def release_seed(request, seed):
    volume, name, content = seed
    with seed_directory(seed_path(volume)) as fd:
        data = read_seed(fd, name)
        if data is None:
            return
        check(data == content, "Guest removed; network seed retained because it changed; inspect")
        check(
            not seed_references(request, name),
            "Guest removed; network seed retained because references remain; inspect",
        )
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)


def check_configuration(request, config, pending, phase):
    expected = desired_config(request)
    check(
        isinstance(config, dict)
        and isinstance(pending, list)
        and all(isinstance(r, dict) for r in pending),
        "Invalid native guest configuration",
    )
    check(
        "lock" not in config and not any("pending" in r or r.get("delete") for r in pending),
        "Guest locked or has pending changes; inspect before retry",
    )
    check(
        config.get("description") == marker(request, phase),
        "Guest ownership/baseline/phase differs; inspect instead of repairing",
    )
    allowed = set(expected) | {
        "description",
        "net0",
        "scsi0",
        "scsi1",
        "efidisk0",
        "digest",
        "meta",
        "smbios1",
        "vmgenid",
        "parent",
    }
    if "cloudinit_snippet_storage" in request["host"]:
        allowed.add("cicustom")
        check(
            config.get("cicustom") == "network=" + seed_volume(request, config)[0],
            "Guest network seed reference differs; inspect drift before retry",
        )
    check(set(config) <= allowed, "Unexpected guest configuration or disks; inspect drift")
    for key, value in expected.items():
        actual = str(config.get(key, "1000" if key == "shares" else None))
        if key == "sshkeys":
            actual = urllib.parse.unquote(actual).strip()
            value = value.strip()
        if key == "ipconfig0":
            actual = template_host.properties(actual)
            value = template_host.properties(value)
        check(actual == value, "Guest managed configuration differs; inspect drift before retry")
    check(
        template_host.properties(config.get("net0"))
        == template_host.properties(desired_network(request, config)),
        "Guest network configuration differs",
    )


def inspect_guest(request, phase="ready", running=True, seed_pending=False, seed=True):
    path = base_path(request)
    config = native(path + "/config")
    check(isinstance(config, dict), "Invalid native guest configuration")
    if "parent" in config:
        check(config["parent"] in snapshot_rows(request), "Guest snapshot ancestry differs")
    checked = config
    if seed_pending:
        check(
            phase == "preparing" and not running and "ide2" in config and "scsi1" not in config,
            "Seed replacement requires the stopped owned clone's original IDE seed",
        )
        checked = {key: value for key, value in config.items() if key != "ide2"}
        checked["scsi1"] = config["ide2"]
    check_configuration(request, checked, native(path + "/pending"), phase)
    if seed and "cloudinit_snippet_storage" in request["host"]:
        verify_seed(request, checked)
    status = native(path + "/status/current")
    check(
        isinstance(status, dict)
        and status.get("status")
        in ({"running", "stopped"} if running is None else {"running" if running else "stopped"}),
        "Guest runtime state differs; inspect exclusive use before retry",
    )
    h = request["host"]
    t = dict(request["template"], template_vmid=h["vmid"], storage=h["storage"])
    t["image"] = dict(t["image"], virtual_size_bytes=h["disk_gib"] * 1024**3)
    storage = native(f"/nodes/{t['node']}/storage/{t['storage']}/status")
    extent = template_host.lvm_extent(t) if storage.get("type") == "lvmthin" else 0
    source = request["template"]
    source_storage = native(f"/nodes/{source['node']}/storage/{source['storage']}/status")
    source_extent = (
        template_host.lvm_extent(source) if source_storage.get("type") == "lvmthin" else 0
    )
    template_host.verify_disks(
        t,
        checked | {"ide2": checked.get("scsi1")},
        extent,
        source_extent=source_extent,
        inherited_seed=seed_pending,
    )
    feature = native(path + "/feature", "--feature", "snapshot")
    check(
        isinstance(feature, dict) and feature.get("hasFeature") == 1,
        "Guest managed disks lack snapshot capability",
    )
    return config


def admission(requests, mode="apply", level="clean"):
    check(isinstance(requests, list) and 0 < len(requests) <= 100, "Invalid selected batch")
    for request in requests:
        validate_request(request)
    hosts = [r["host"] for r in requests]
    check(len({h["vmid"] for h in hosts}) == len(hosts), "Duplicate selected guest IDs")
    check(len({h["proxmox_node"] for h in hosts}) == 1, "Native batch spans nodes")
    check(
        not {h["vmid"] for h in hosts} & {h["template_vmid"] for h in hosts},
        "Selected guest/template IDs collide",
    )
    storages = {}
    source_extents = {}
    for r in requests:
        t = r["template"]
        h = r["host"]
        source_storage = template_host.host_admission(t, network=False)
        source_extents[t["template_vmid"]] = source_storage["extent_bytes"]
        storages[h["storage"]] = template_host.host_admission(
            dict(t, storage=h["storage"], bridge=h["bridge"], vlan=h.get("vlan"))
        )
        refuse(template_host.existing_resource(t), "template-absent", "Selected template is absent")
        marker_value = "kdive-template-v1:" + template_host.identity(t) + ":ready"
        config = template_host.verify_configuration(t, marker_value, source_storage["extent_bytes"])
        check(config.get("template") == 1, "Selected source is not a ready template")
        if "cloudinit_snippet_storage" in h and mode not in {"teardown", "plan-teardown"}:
            seed_storage_admission(h)
    if mode in {"teardown", "plan-teardown"}:
        resources = native("/cluster/resources", "--type", "vm")
        existing = [resource_present(r, resources) for r in requests]
        for r, present in zip(requests, existing, strict=True):
            if present:
                inspect_removable(r)
        return existing
    flags = Path("/proc/cpuinfo").read_text().split()
    module = "kvm_intel" if "vmx" in flags else "kvm_amd" if "svm" in flags else None
    refuse(
        module is not None,
        "nesting-disabled",
        "Host virtualization exposure absent; operator must configure nesting",
    )
    nested = Path(f"/sys/module/{module}/parameters/nested")
    refuse(
        nested.is_file() and nested.read_text().strip().lower() in {"1", "y"},
        "nesting-disabled",
        "Host nesting disabled; operator must configure it before provisioning",
    )
    guest_verify.kvm_probe()
    resources = native("/cluster/resources", "--type", "vm")
    existing = [resource_present(r, resources) for r in requests]
    for request, present in zip(requests, existing, strict=True):
        if present:
            config = inspect_guest(request, running=None if mode.endswith("restore") else True)
            if mode.endswith("level"):
                prepare_admission(request, config, level)
            elif mode.endswith("restore"):
                rollback_admission(request, config, level)
            else:
                baseline(request, config, level)
    if mode.endswith("level"):
        refuse(all(existing), "guests-missing", "Level preparation requires existing owned guests")
        return existing
    if mode.endswith("restore"):
        refuse(
            all(existing),
            "guests-missing",
            "Restore requires existing owned guests and clean baseline",
        )
        return existing
    fresh = [r for r, present in zip(requests, existing, strict=True) if not present]
    for r in fresh:
        refuse(
            r["host"]["disk_gib"] * 1024**3
            >= template_host.allocated_size(
                r["template"]["image"]["virtual_size_bytes"],
                source_extents[r["host"]["template_vmid"]],
            ),
            "disk-too-small",
            "Guest root disk cannot shrink the source allocation; increase disk_gib",
        )
    refuse(
        mode != "verify" or all(existing), "guests-missing", "Verify requires existing ready guests"
    )
    for warning in check_capacity(
        fresh,
        native(f"/nodes/{hosts[0]['proxmox_node']}/status"),
        Path("/proc/meminfo").read_text(),
        storages,
        source_extents,
    ):
        print(json.dumps({"phase": "capacity-warning"} | warning), flush=True)
    return existing


def clone(request):
    h = request["host"]
    refuse(
        not resource_present(request, native("/cluster/resources", "--type", "vm")),
        "vmid-in-use",
        "Guest ID became occupied; select an unused ID",
    )
    command(
        [
            "qm",
            "clone",
            str(h["template_vmid"]),
            str(h["vmid"]),
            "--full",
            "1",
            "--storage",
            h["storage"],
            "--name",
            h["fqdn"].split(".")[0],
            "--description",
            marker(request, "preparing"),
        ],
        timeout=1800,
    )
    config = native(base_path(request) + "/config")
    check(
        isinstance(config, dict)
        and config.get("description") == marker(request, "preparing")
        and config.get("template", 0) == 0
        and "lock" not in config,
        "New clone ownership/state differs; inspect partial allocation",
    )
    options = desired_config(request)
    options["net0"] = desired_network(request, config)
    if "cloudinit_snippet_storage" in h:
        options["cicustom"] = "network=" + write_seed(request, config)
    with tempfile.NamedTemporaryFile(mode="w", prefix="kdive-guest-keys-") as keys:
        keys.write(options.pop("sshkeys"))
        keys.flush()
        options["sshkeys"] = keys.name
        argv = ["qm", "set", str(h["vmid"])]
        for key, value in options.items():
            argv.extend(["--" + key, value])
        command(argv)
    command(["qm", "resize", str(h["vmid"]), "scsi0", str(h["disk_gib"]) + "G"], timeout=600)
    config = inspect_guest(request, phase="preparing", running=False, seed_pending=True)
    check(
        isinstance(config.get("digest"), str)
        and re.fullmatch(r"[a-f0-9]{40}", config["digest"]) is not None,
        "Missing native configuration digest before seed replacement",
    )
    command(["qm", "set", str(h["vmid"]), "--delete", "ide2", "--digest", config["digest"]])
    detached = native(base_path(request) + "/config")
    preserved = {key: value for key, value in config.items() if key not in {"ide2", "digest"}}
    check(
        isinstance(detached, dict)
        and {key: value for key, value in detached.items() if key != "digest"} == preserved
        and isinstance(detached.get("digest"), str)
        and re.fullmatch(r"[a-f0-9]{40}", detached["digest"]) is not None,
        "Guest changed during seed replacement; inspect retained partial",
    )
    command(
        [
            "qm",
            "set",
            str(h["vmid"]),
            "--scsi1",
            h["storage"] + ":cloudinit",
            "--digest",
            detached["digest"],
        ]
    )
    attached = inspect_guest(request, phase="preparing", running=False)
    check(
        {key: value for key, value in attached.items() if key not in {"scsi1", "digest"}}
        == preserved,
        "Guest changed during seed replacement; inspect retained partial",
    )
    command(["qm", "start", str(h["vmid"])], timeout=180)


def verify_ack(request, ack, phase="prepared"):
    check(
        isinstance(ack, dict)
        and set(ack) == {"vmid", "identity", "verified", "phase"}
        and ack["phase"] == phase
        and type(ack["vmid"]) is int
        and ack["vmid"] == request["host"]["vmid"]
        and ack["identity"] == identity(request)
        and ack["verified"] is True,
        "Readiness acknowledgement mismatch; guest remains preparing",
    )


def emit(request, phase, **values):
    print(
        json.dumps(
            {"vmid": request["host"]["vmid"], "identity": identity(request), "phase": phase}
            | values
        ),
        flush=True,
    )


def read_line(timeout):
    deadline = time.monotonic() + timeout
    line = bytearray()
    while not line.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        check(
            remaining > 0 and select.select([sys.stdin], [], [], remaining)[0],
            "Controller acknowledgement timed out; inspect preparing guest",
        )
        part = os.read(sys.stdin.fileno(), 1)
        check(
            part and len(line) < 65536,
            "Controller disconnected or response exceeded bound; inspect preparing guest",
        )
        line.extend(part)
    return json.loads(line)


def verify_readiness(request, fresh):
    phase = "preparing" if fresh else "ready"
    config = inspect_guest(request, phase=phase)
    guest_uuid = guest_verify.machine_uuid(
        template_host.properties(config.get("smbios1")).get("uuid")
    )
    emit(request, "prepared", fresh=fresh, guest_uuid=guest_uuid)
    # SSH 600s + guest RPC 1800s + transport margin.
    verify_ack(request, read_line(2500))
    if fresh and request["host"]["profile"] == "opensuse":
        check(
            inspect_guest(request, phase=phase) == config,
            "Guest configuration changed before reboot; inspect retained partial",
        )
        emit(request, "reboot", guest_uuid=guest_uuid)
        # One reboot RPC 60s + strict reconnect 600s + read-only RPC 1800s + margin.
        verify_ack(request, read_line(2700), phase="post-reboot")
    command(["qm", "agent", str(request["host"]["vmid"]), "ping"], timeout=60)
    ready_input = inspect_guest(request, phase=phase)
    check(ready_input == config, "Guest configuration changed during readiness")
    if fresh:
        check(
            isinstance(config.get("digest"), str)
            and re.fullmatch(r"[a-f0-9]{40}", config["digest"]),
            "Missing native readiness digest",
        )
        command(
            [
                "qm",
                "set",
                str(request["host"]["vmid"]),
                "--description",
                marker(request, "ready"),
                "--digest",
                config["digest"],
            ]
        )
    result = inspect_guest(request)
    check(
        {k: v for k, v in result.items() if k not in {"description", "digest"}}
        == {k: v for k, v in config.items() if k not in {"description", "digest"}},
        "Guest configuration changed during ready marking",
    )
    return result


def normalized_config(config, snapshot=False):
    check(isinstance(config, dict), "Invalid native baseline configuration")
    if "vmgenid" in config:
        guest_verify.machine_uuid(config["vmgenid"])
    ignored = {"digest", "description", "parent", "vmgenid"}
    if snapshot:
        check(
            not {"vmstate", "snapstate"} & set(config),
            "Incomplete or RAM snapshot; recreate explicitly",
        )
        ignored.add("snaptime")
    return {key: value for key, value in config.items() if key not in ignored} | {
        "shares": str(config.get("shares", "1000"))
    }


def snapshot_rows(request):
    rows = native(base_path(request) + "/snapshot")
    check(isinstance(rows, list) and rows, "Invalid native snapshot inventory")
    result = {}
    for row in rows:
        check(
            isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", row["name"])
            and row["name"] not in result,
            "Invalid or duplicate snapshot identity",
        )
        result[row["name"]] = row
    check("current" in result, "Native snapshot inventory lacks current state")
    del result["current"]
    return result


def clean_baseline(request, config):
    rows = snapshot_rows(request)
    check(
        "clean" in rows, "Clean baseline missing; explicitly teardown and recreate selected guest"
    )
    row = rows["clean"]
    check(
        type(row.get("snaptime")) is int
        and row["snaptime"] > 0
        and row.get("vmstate") == 0
        and "snapstate" not in row,
        "Incomplete or RAM clean baseline; inspect and recreate explicitly",
    )
    snapshot = native(base_path(request) + "/snapshot/clean/config")
    check(isinstance(snapshot, dict), "Invalid clean snapshot configuration")
    try:
        metadata = json.loads(row.get("description", ""))
    except (ValueError, TypeError):
        raise GuestError("Invalid clean baseline metadata; recreate explicitly") from None
    expected = {
        "schema": 1,
        "identity": identity(request),
        "config_sha256": template_host.digest(normalized_config(config)),
    }
    check(
        isinstance(metadata, dict)
        and type(metadata.get("schema")) is int
        and metadata == expected
        and snapshot.get("description") == row.get("description")
        and snapshot.get("snaptime") == row["snaptime"]
        and normalized_config(snapshot, snapshot=True) == normalized_config(config),
        "Clean baseline identity/configuration differs; recreate explicitly",
    )
    return {
        "snapshot": "clean",
        "snapshot_identity": template_host.digest(metadata),
        "snapshot_config_sha256": metadata["config_sha256"],
        "snapshot_time": row["snaptime"],
    }


def level_metadata_chain(request, config, level):
    entries = guest_verify.level_chain(level)
    evidence = clean_baseline(request, config)
    rows = snapshot_rows(request)
    clean = guest_verify.level_json(rows["clean"]["description"])
    check(
        template_host.digest(clean) == evidence["snapshot_identity"],
        "Clean metadata changed during admission",
    )
    chain = [clean]
    for item in entries:
        name = item["name"]
        check(
            name in rows,
            "Level snapshot missing; prepare and ask operator to capture selected level",
        )
        row = rows[name]
        check(
            type(row.get("snaptime")) is int
            and row["snaptime"] > 0
            and row.get("vmstate") == 0
            and "snapstate" not in row,
            "Incomplete or RAM level snapshot; inspect and re-prepare",
        )
        snapshot = native(base_path(request) + "/snapshot/" + name + "/config")
        metadata = guest_verify.level_json(row.get("description"))
        guest_verify.level_metadata(
            metadata, name, chain[-1], identity(request), clean["config_sha256"]
        )
        check(
            isinstance(snapshot, dict)
            and snapshot.get("description") == row["description"]
            and snapshot.get("snaptime") == row["snaptime"]
            and normalized_config(snapshot, snapshot=True) == normalized_config(config),
            "Level snapshot configuration differs; re-prepare selected level",
        )
        check(
            row.get("parent") == item["parent"] and snapshot.get("parent") == item["parent"],
            "Level native parent differs; re-prepare selected level",
        )
        chain.append(metadata)
    return chain


def baseline(request, config, level="clean"):
    if level == "clean":
        return clean_baseline(request, config)
    metadata = level_metadata_chain(request, config, level)[-1]
    row = snapshot_rows(request)[level]
    check(
        guest_verify.level_equal(guest_verify.level_json(row["description"]), metadata),
        "Level metadata changed during admission",
    )
    return {
        "snapshot": level,
        "snapshot_identity": template_host.digest(metadata),
        "snapshot_config_sha256": metadata["config_sha256"],
        "snapshot_time": row["snaptime"],
    }


def rollback_admission(request, config, level):
    baseline(request, config, level)
    h = request["host"]
    storage = native(f"/nodes/{h['proxmox_node']}/storage/{h['storage']}/status")
    check(
        isinstance(storage, dict) and storage.get("type") in {"zfspool", "lvmthin"},
        "Unknown rollback storage; inspect native storage",
    )
    if storage["type"] == "zfspool":
        rows = snapshot_rows(request)
        target = rows[level]["snaptime"]
        check(
            all(type(row.get("snaptime")) is int and row["snaptime"] > 0 for row in rows.values()),
            "Invalid ZFS snapshot times; inspect native snapshots",
        )
        check(
            not any(
                name != level and (row["snaptime"] >= target or row.get("parent") == level)
                for name, row in rows.items()
            ),
            "ZFS rollback blocked by newer snapshots; "
            "operator must inspect and remove them before retry",
        )


def prepare_admission(request, config, level):
    entries = guest_verify.level_chain(level)
    check(entries, "Clean preparation is fresh provisioning only")
    parent = entries[-1]["parent"]
    baseline(request, config, parent)
    check(
        config.get("parent") == parent, "Current guest parent differs; restore parent level first"
    )
    check(
        level not in snapshot_rows(request),
        "Level snapshot already exists; operator must remove it before re-preparation",
    )


def level_exchange(request, config, level, prepare=False):
    parent = guest_verify.level_chain(level)[-1]["parent"] if prepare else level
    chain = level_metadata_chain(request, config, parent)
    proposed = None
    if prepare:
        proposed = {
            "schema": 1,
            "level": level,
            "parent": parent,
            "parent_identity": template_host.digest(chain[-1]),
            "identity": identity(request),
            "config_sha256": chain[0]["config_sha256"],
            "content": {},
        }
    emit(
        request,
        "levels",
        guest_uuid=guest_verify.machine_uuid(
            template_host.properties(config.get("smbios1")).get("uuid")
        ),
        level=level,
        prepare=prepare,
        chain=chain,
        proposed=proposed,
    )
    ack = read_line(guest_verify.KDIVE_TIMEOUT + 700 if prepare and level == "kdive" else 2500)
    if prepare:
        check(isinstance(ack, dict) and "metadata" in ack, "Missing prepared level metadata")
        metadata = ack.pop("metadata")
        guest_verify.level_metadata(
            metadata, level, chain[-1], identity(request), chain[0]["config_sha256"]
        )
    verify_ack(request, ack, phase="levels")
    current = inspect_guest(request)
    check(
        normalized_config(current) == normalized_config(config), "Guest changed during level checks"
    )
    if not prepare:
        check(
            guest_verify.level_equal(level_metadata_chain(request, current, level), chain),
            "Level chain changed during guest checks",
        )
        return None
    prepare_admission(request, current, level)
    stopped = shutdown_guest(request, current)
    prepare_admission(request, stopped, level)
    check(
        guest_verify.level_equal(level_metadata_chain(request, stopped, parent), chain),
        "Parent metadata changed during preparation",
    )
    return metadata


def shutdown_guest(request, config, phase="ready", seed=True):
    status = native(base_path(request) + "/status/current")
    check(
        isinstance(status, dict) and status.get("status") in {"running", "stopped"},
        "Invalid guest runtime state before shutdown",
    )
    if status["status"] == "running":
        command(
            [
                "qm",
                "shutdown",
                str(request["host"]["vmid"]),
                "--timeout",
                "180",
                "--forceStop",
                "0",
            ],
            timeout=210,
        )
    stopped = inspect_guest(request, phase=phase, running=False, seed=seed)
    check(normalized_config(stopped) == normalized_config(config), "Guest changed during shutdown")
    return stopped


def capture_baseline(request, config):
    check(
        not snapshot_rows(request), "Fresh guest already has snapshots; inspect without replacement"
    )
    stopped = shutdown_guest(request, config)
    check(not snapshot_rows(request), "Snapshot appeared during shutdown; inspect retained guest")
    metadata = {
        "schema": 1,
        "identity": identity(request),
        "config_sha256": template_host.digest(normalized_config(stopped)),
    }
    command(
        [
            "qm",
            "snapshot",
            str(request["host"]["vmid"]),
            "clean",
            "--vmstate",
            "0",
            "--description",
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        ],
        timeout=1800,
    )
    baseline(request, inspect_guest(request, running=False))
    command(["qm", "start", str(request["host"]["vmid"])], timeout=180)
    config = inspect_guest(request)
    emit(
        request,
        "baseline-boot",
        guest_uuid=guest_verify.machine_uuid(
            template_host.properties(config.get("smbios1")).get("uuid")
        ),
    )
    verify_ack(request, read_line(2500), phase="baseline-boot")
    command(["qm", "agent", str(request["host"]["vmid"]), "ping"], timeout=60)
    result = inspect_guest(request)
    check(
        normalized_config(result) == normalized_config(config), "Guest changed during baseline boot"
    )
    baseline(request, result)
    return result


def inspect_removable(request):
    config = native(base_path(request) + "/config")
    check(isinstance(config, dict), "Invalid selected guest configuration")
    phase = next(
        (p for p in ("ready", "preparing") if config.get("description") == marker(request, p)), None
    )
    refuse(
        phase is not None, "ownership-differs", "Selected guest ownership differs; teardown refused"
    )
    config = inspect_guest(request, phase=phase, running=None, seed=False)
    for name in snapshot_rows(request):
        snapshot = native(base_path(request) + "/snapshot/" + name + "/config")
        normalized = normalized_config(snapshot, snapshot=True)
        # Deletion may free disks referenced only by an older snapshot.
        check(
            normalized == normalized_config(config),
            "Snapshot references differ; inspect before teardown",
        )
    return config, phase


def lifecycle(request, mode, level="clean"):
    vmid = str(request["host"]["vmid"])
    if mode == "restore":
        config = inspect_guest(request, running=None)
        rollback_admission(request, config, level)
        stopped = shutdown_guest(request, config)
        rollback_admission(request, stopped, level)
        command(["qm", "rollback", vmid, level, "--start", "0"], timeout=1800)
        baseline(request, inspect_guest(request, running=False), level)
        command(["qm", "start", vmid], timeout=180)
        return verify_readiness(request, fresh=False)
    config, phase = inspect_removable(request)
    seed = seed_volume(request, config) if "cloudinit_snippet_storage" in request["host"] else None
    volumes = {config[slot].split(",", 1)[0] for slot in ("scsi0", "scsi1", "efidisk0")}
    shutdown_guest(request, config, phase, seed=False)
    inspect_removable(request)
    command(
        ["qm", "destroy", vmid, "--purge", "0", "--destroy-unreferenced-disks", "0"], timeout=1800
    )
    check(
        not resource_present(request, native("/cluster/resources", "--type", "vm")),
        "Guest remains after teardown; inspect completed task",
    )
    rows = native(
        f"/nodes/{request['host']['proxmox_node']}/storage/{request['host']['storage']}/content"
    )
    check(
        isinstance(rows, list)
        and all(isinstance(r, dict) for r in rows)
        and not any(
            r.get("volid") in volumes or r.get("vmid") == request["host"]["vmid"] for r in rows
        ),
        "Owned volumes remain after teardown; inspect without broad cleanup",
    )
    if seed:
        try:
            release_seed(request, seed)
        except (GuestError, OSError) as error:
            if str(error).startswith("Guest removed"):
                raise
            raise GuestError(
                "Guest removed; network seed retained; inspect the snippet storage"
            ) from None
    return None


def session(requests, mode, confirmed=False, exclusive=False, level="clean"):
    check(
        mode
        in {
            "plan",
            "apply",
            "verify",
            "restore",
            "teardown",
            "plan-restore",
            "plan-teardown",
            "level",
            "plan-level",
        },
        "Invalid guest operation",
    )
    guest_verify.level_chain(level)
    check(
        level == "clean" or mode in {"verify", "restore", "plan-restore", "level", "plan-level"},
        "LEVEL is only supported for verify, restore and level",
    )
    check(
        not mode.endswith("level") or level != "clean",
        "Clean preparation is fresh provisioning only",
    )
    check(type(confirmed) is bool and type(exclusive) is bool, "Invalid destructive intent")
    check(
        mode not in {"restore", "teardown", "level"} or confirmed and exclusive,
        "Destructive operation requires exact selected confirmation and exclusive use",
    )
    check(isinstance(requests, list) and 0 < len(requests) <= 100, "Invalid selected batch")
    for request in requests:
        validate_request(request)
    with contextlib.ExitStack() as locks:
        if not mode.startswith("plan"):
            locks.enter_context(template_host.template_lock(0))
            for vmid in sorted(
                {r["host"][key] for r in requests for key in ("vmid", "template_vmid")}
            ):
                locks.enter_context(template_host.template_lock(vmid))
        existing = admission(requests, mode, level)
        for request, present in zip(requests, existing, strict=True):
            started = time.monotonic()
            if mode.startswith("plan"):
                action = {
                    "plan": "preserved" if present else "would-create",
                    "plan-restore": "would-restore",
                    "plan-level": "would-prepare-level",
                    "plan-teardown": "would-destroy" if present else "absent",
                }[mode]
                emit(request, "planned", action=action)
                continue
            if mode == "teardown":
                if present:
                    lifecycle(request, mode)
                emit(
                    request,
                    "removed",
                    action="destroyed" if present else "absent",
                    duration_seconds=round(time.monotonic() - started, 3),
                )
                continue
            if mode == "restore":
                config = lifecycle(request, mode, level)
            else:
                if not present:
                    clone(request)
                config = verify_readiness(request, fresh=not present)
                if not present:
                    config = capture_baseline(request, config)
            if mode == "level":
                metadata = level_exchange(request, config, level, prepare=True)
                emit(request, "snapshot-ready", level=level, metadata=metadata)
                continue
            if level != "clean":
                level_exchange(request, config, level)
            action = "preserved" if present else "created"
            if mode == "restore":
                action = "restored"
            emit(
                request,
                "ready",
                action=action,
                config_sha256=template_host.digest(config),
                duration_seconds=round(time.monotonic() - started, 3),
                **baseline(request, config, level),
            )


def main():
    try:
        envelope = read_line(30)
        check(
            isinstance(envelope, dict)
            and set(envelope) == {"requests", "mode", "confirmed", "exclusive", "level"},
            "Invalid native guest envelope",
        )
        session(
            envelope["requests"],
            envelope["mode"],
            envelope["confirmed"],
            envelope["exclusive"],
            envelope["level"],
        )
    except (GuestError, guest_verify.GuestError) as error:
        event = {"error": str(error)}
        if isinstance(error, ReasonError):
            event["code"] = error.code
        print(json.dumps(event), flush=True)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            json.dumps(
                {"error": "Invalid native state; inspect selected resources; no cleanup attempted"}
            ),
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
