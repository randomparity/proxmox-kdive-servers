"""Owned native guest lifecycle, held through controller-confirmed readiness."""

import contextlib
import json
import math
import os
import re
import select
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
        isinstance(request, dict) and set(request) == {"host", "template", "revision"},
        "Invalid guest request",
    )
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
        return
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
    check(
        sum(r["host"]["cores"] for r in requests) + math.ceil(max(cpu * cpus, load)) <= cpus,
        "Insufficient observed CPU capacity; release workload capacity before retry",
    )
    match = re.search(r"^MemAvailable:\s+([0-9]+) kB$", memory, re.MULTILINE)
    check(match is not None, "Missing MemAvailable; inspect native memory metrics")
    check(
        sum(r["host"]["memory_mib"] for r in requests) * 1024 <= int(match[1]),
        "Insufficient available RAM; release memory before retry",
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
        check(
            type(status.get("avail")) is int and status["avail"] >= demand,
            "Insufficient reported storage space; release storage before retry",
        )


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
        check(
            matches[0]["node"] == h["proxmox_node"] and matches[0]["type"] == "qemu",
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


def desired_network(request, config):
    h = request["host"]
    net = template_host.properties(config.get("net0"))
    models = set(net) & NIC_MODELS
    check(len(models) == 1, "Missing cloned NIC model; inspect retained guest")
    mac = net[models.pop()]
    check(
        isinstance(mac, str) and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac),
        "Missing cloned NIC identity; inspect retained guest",
    )
    value = f"{h.get('nic_model', 'virtio')}={mac},bridge={h['bridge']}"
    if "vlan" in h:
        value += f",tag={h['vlan']}"
    if "nic_queues" in h:
        value += f",queues={h['nic_queues']}"
    return value


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


def inspect_guest(request, phase="ready", running=True, seed_pending=False):
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


def admission(requests, mode="apply"):
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
        check(template_host.existing_resource(t), "Selected template is absent")
        marker_value = "kdive-template-v1:" + template_host.identity(t) + ":ready"
        config = template_host.verify_configuration(t, marker_value, source_storage["extent_bytes"])
        check(config.get("template") == 1, "Selected source is not a ready template")
    if mode in {"teardown", "plan-teardown"}:
        resources = native("/cluster/resources", "--type", "vm")
        existing = [resource_present(r, resources) for r in requests]
        for r, present in zip(requests, existing, strict=True):
            if present:
                inspect_removable(r)
        return existing
    flags = Path("/proc/cpuinfo").read_text().split()
    module = "kvm_intel" if "vmx" in flags else "kvm_amd" if "svm" in flags else None
    check(
        module is not None, "Host virtualization exposure absent; operator must configure nesting"
    )
    nested = Path(f"/sys/module/{module}/parameters/nested")
    check(
        nested.is_file() and nested.read_text().strip().lower() in {"1", "y"},
        "Host nesting disabled; operator must configure it before provisioning",
    )
    guest_verify.kvm_probe()
    resources = native("/cluster/resources", "--type", "vm")
    existing = [resource_present(r, resources) for r in requests]
    for request, present in zip(requests, existing, strict=True):
        if present:
            config = inspect_guest(request, running=None if mode.endswith("restore") else True)
            baseline(request, config)
    if mode.endswith("restore"):
        check(all(existing), "Restore requires existing owned guests and clean baseline")
        return existing
    fresh = [r for r, present in zip(requests, existing, strict=True) if not present]
    for r in fresh:
        check(
            r["host"]["disk_gib"] * 1024**3
            >= template_host.allocated_size(
                r["template"]["image"]["virtual_size_bytes"],
                source_extents[r["host"]["template_vmid"]],
            ),
            "Guest root disk cannot shrink the source allocation; increase disk_gib",
        )
    check_capacity(
        fresh,
        native(f"/nodes/{hosts[0]['proxmox_node']}/status"),
        Path("/proc/meminfo").read_text(),
        storages,
        source_extents,
    )
    return existing


def clone(request):
    h = request["host"]
    check(
        not resource_present(request, native("/cluster/resources", "--type", "vm")),
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


def baseline(request, config):
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


def shutdown_guest(request, config, phase="ready"):
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
    stopped = inspect_guest(request, phase=phase, running=False)
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
    check(phase is not None, "Selected guest ownership differs; teardown refused")
    config = inspect_guest(request, phase=phase, running=None)
    for name in snapshot_rows(request):
        snapshot = native(base_path(request) + "/snapshot/" + name + "/config")
        normalized = normalized_config(snapshot, snapshot=True)
        # Deletion may free disks referenced only by an older snapshot.
        check(
            normalized == normalized_config(config),
            "Snapshot references differ; inspect before teardown",
        )
    return config, phase


def lifecycle(request, mode):
    vmid = str(request["host"]["vmid"])
    if mode == "restore":
        config = inspect_guest(request, running=None)
        baseline(request, config)
        stopped = shutdown_guest(request, config)
        baseline(request, stopped)
        command(["qm", "rollback", vmid, "clean", "--start", "0"], timeout=1800)
        baseline(request, inspect_guest(request, running=False))
        command(["qm", "start", vmid], timeout=180)
        config = verify_readiness(request, fresh=False)
        return config
    config, phase = inspect_removable(request)
    volumes = {config[slot].split(",", 1)[0] for slot in ("scsi0", "scsi1", "efidisk0")}
    shutdown_guest(request, config, phase)
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
    return None


def session(requests, mode, confirmed=False, exclusive=False):
    check(
        mode in {"plan", "apply", "verify", "restore", "teardown", "plan-restore", "plan-teardown"},
        "Invalid guest operation",
    )
    check(type(confirmed) is bool and type(exclusive) is bool, "Invalid destructive intent")
    check(
        mode not in {"restore", "teardown"} or confirmed and exclusive,
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
        existing = admission(requests, mode)
        check(mode != "verify" or all(existing), "Verify requires existing ready guests")
        for request, present in zip(requests, existing, strict=True):
            started = time.monotonic()
            if mode.startswith("plan"):
                action = {
                    "plan": "preserved" if present else "would-create",
                    "plan-restore": "would-restore",
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
                config = lifecycle(request, mode)
            else:
                if not present:
                    clone(request)
                config = verify_readiness(request, fresh=not present)
                if not present:
                    config = capture_baseline(request, config)
            emit(
                request,
                "ready",
                action="restored" if mode == "restore" else "preserved" if present else "created",
                config_sha256=template_host.digest(config),
                duration_seconds=round(time.monotonic() - started, 3),
                **baseline(request, config),
            )


def main():
    try:
        envelope = read_line(30)
        check(
            isinstance(envelope, dict)
            and set(envelope) == {"requests", "mode", "confirmed", "exclusive"},
            "Invalid native guest envelope",
        )
        session(
            envelope["requests"], envelope["mode"], envelope["confirmed"], envelope["exclusive"]
        )
    except (GuestError, guest_verify.GuestError) as error:
        print(json.dumps({"error": str(error)}), flush=True)
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
