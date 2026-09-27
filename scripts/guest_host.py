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
    from .validate_inventory import validate_host
else:
    import guest_verify
    import template_host
    from validate_inventory import validate_host

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
    return template_host.digest(
        {
            "schema": 1,
            "management": 1,
            "template": template_host.identity(request["template"]),
            "guest": {key: request["host"][key] for key in BASELINE_FIELDS},
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
                ("storage", "storage"),
                ("bridge", "bridge"),
                ("cpu", "cpu"),
            )
        )
        and h.get("vlan") == t["vlan"],
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


def check_capacity(requests, node, memory, storages):
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
            + 2 * template_host.allocated_size(8 * 1024**2, extent)
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
    return template_host.FIXED | {
        "name": h["fqdn"].split(".")[0],
        "searchdomain": h["fqdn"].split(".", 1)[1],
        "cores": str(h["cores"]),
        "memory": str(h["memory_mib"]),
        "balloon": "0",
        "agent": "1",
        "ciupgrade": "0",
        "ciuser": h["ansible_user"],
        "nameserver": " ".join(h["dns_servers"]),
        "ipconfig0": f"ip={h['ipv4_cidr']},gw={h['gateway']}",
        "sshkeys": "\n".join(h["ssh_public_keys"]) + "\n",
    }


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
        "ide2",
        "efidisk0",
        "digest",
        "meta",
        "smbios1",
        "vmgenid",
    }
    check(set(config) <= allowed, "Unexpected guest configuration or disks; inspect drift")
    for key, value in expected.items():
        actual = str(config.get(key))
        if key == "sshkeys":
            actual = urllib.parse.unquote(actual).strip()
            value = value.strip()
        if key == "ipconfig0":
            actual = template_host.properties(actual)
            value = template_host.properties(value)
        check(actual == value, "Guest managed configuration differs; inspect drift before retry")
    net = template_host.properties(config.get("net0"))
    h = request["host"]
    check(
        set(net) == {"virtio", "bridge"} | ({"tag"} if "vlan" in h else set())
        and net.get("bridge") == h["bridge"]
        and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", net.get("virtio") or "")
        and ("vlan" not in h or net.get("tag") == str(h["vlan"])),
        "Guest network configuration differs",
    )


def inspect_guest(request, phase="ready", running=True):
    path = base_path(request)
    config = native(path + "/config")
    check_configuration(request, config, native(path + "/pending"), phase)
    status = native(path + "/status/current")
    check(
        isinstance(status, dict) and status.get("status") == ("running" if running else "stopped"),
        "Guest runtime state differs; inspect exclusive use before retry",
    )
    h = request["host"]
    t = dict(request["template"], template_vmid=h["vmid"])
    t["image"] = dict(t["image"], virtual_size_bytes=h["disk_gib"] * 1024**3)
    storage = native(f"/nodes/{t['node']}/storage/{t['storage']}/status")
    extent = template_host.lvm_extent(t) if storage.get("type") == "lvmthin" else 0
    template_host.verify_disks(t, config, extent)
    feature = native(path + "/feature", "--feature", "snapshot")
    check(
        isinstance(feature, dict) and feature.get("hasFeature") == 1,
        "Guest managed disks lack snapshot capability",
    )
    return config


def admission(requests):
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
    for r in requests:
        t = r["template"]
        storages[t["storage"]] = template_host.host_admission(t)
        check(template_host.existing_resource(t), "Selected template is absent")
        marker_value = "kdive-template-v1:" + template_host.identity(t) + ":ready"
        config = template_host.verify_configuration(
            t, marker_value, storages[t["storage"]]["extent_bytes"]
        )
        check(config.get("template") == 1, "Selected source is not a ready template")
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
            inspect_guest(request)
    fresh = [r for r, present in zip(requests, existing, strict=True) if not present]
    check_capacity(
        fresh,
        native(f"/nodes/{hosts[0]['proxmox_node']}/status"),
        Path("/proc/meminfo").read_text(),
        storages,
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
    with tempfile.NamedTemporaryFile(mode="w", prefix="kdive-guest-keys-") as keys:
        keys.write(options.pop("sshkeys"))
        keys.flush()
        options["sshkeys"] = keys.name
        argv = ["qm", "set", str(h["vmid"])]
        for key, value in options.items():
            argv.extend(["--" + key, value])
        command(argv)
    command(["qm", "resize", str(h["vmid"]), "scsi0", str(h["disk_gib"]) + "G"], timeout=600)
    inspect_guest(request, phase="preparing", running=False)
    command(["qm", "start", str(h["vmid"])], timeout=180)


def verify_ack(request, ack):
    check(
        isinstance(ack, dict)
        and set(ack) == {"vmid", "identity", "verified"}
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


def session(requests, mode):
    check(mode in {"plan", "apply", "verify"}, "Invalid guest operation")
    check(isinstance(requests, list) and 0 < len(requests) <= 100, "Invalid selected batch")
    for request in requests:
        validate_request(request)
    with contextlib.ExitStack() as locks:
        if mode == "apply":
            locks.enter_context(template_host.template_lock(0))
            for vmid in sorted(
                {r["host"][key] for r in requests for key in ("vmid", "template_vmid")}
            ):
                locks.enter_context(template_host.template_lock(vmid))
        existing = admission(requests)
        check(mode != "verify" or all(existing), "Verify requires existing ready guests")
        for request, present in zip(requests, existing, strict=True):
            started = time.monotonic()
            if mode == "plan":
                emit(request, "planned", action="preserved" if present else "would-create")
                continue
            if not present:
                clone(request)
            emit(request, "prepared", fresh=not present)
            verify_ack(request, read_line(1800))
            command(["qm", "agent", str(request["host"]["vmid"]), "ping"], timeout=60)
            if not present:
                inspect_guest(request, phase="preparing")
                command(
                    [
                        "qm",
                        "set",
                        str(request["host"]["vmid"]),
                        "--description",
                        marker(request, "ready"),
                    ]
                )
            config = inspect_guest(request)
            emit(
                request,
                "ready",
                action="preserved" if present else "created",
                config_sha256=template_host.digest(config),
                duration_seconds=round(time.monotonic() - started, 3),
            )


def main():
    try:
        envelope = read_line(30)
        check(
            isinstance(envelope, dict) and set(envelope) == {"requests", "mode"},
            "Invalid native guest envelope",
        )
        session(envelope["requests"], envelope["mode"])
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
