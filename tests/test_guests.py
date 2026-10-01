"""Exercise selected guest admission, ownership, readiness and private transport."""

import ast
import contextlib
import copy
import errno
import io
import itertools
import json
import os
import select
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import guest_host, guest_verify, guests
from scripts.validate_inventory import ValidationError, load_inventory, managed_hosts
from tests.test_templates import NativeFixture

ROOT = Path(__file__).resolve().parents[1]


def request():
    host = managed_hosts(load_inventory(ROOT / "inventory/example.yml"))["ubuntu"]
    host.update(storage="pool", cores=2, memory_mib=4096, disk_gib=32)
    host.pop("vlan", None)
    source = {
        "template": 1,
        "scsi0": "pool:base-9001-disk-0,size=4G",
        "net0": "virtio=02:11:22:33:44:55,bridge=vmbr0",
        "description": "kdive-template-v1:"
        + guest_host.template_host.identity(guests.templates.request_for(host, False, False))
        + ":ready",
    }
    return guests.request_for(host, "a" * 40, source)


class TestGuestHost(unittest.TestCase):
    def setUp(self):
        self.request = request()
        self.host = self.request["host"]

    def test_capacity_checks_observed_load_and_whole_batch(self):
        node = {"cpuinfo": {"cpus": 4}, "cpu": 0.1, "loadavg": ["1.1", "0", "0"]}
        memory = "MemAvailable: 8388608 kB\n"
        storage = {"avail": 100 * 1024**3, "extent_bytes": 0}
        pools = {"pool": storage}
        self.assertEqual(guest_host.check_capacity([self.request], node, memory, pools), [])
        self.assertEqual(guest_host.check_capacity([], {}, "", {}), [])
        for changed in [
            dict(node, cpu=None),
            dict(node, cpu=1.1),
            dict(node, loadavg=[]),
            dict(node, loadavg=["nan"]),
        ]:
            with self.subTest(node=changed), self.assertRaises(guest_host.GuestError):
                guest_host.check_capacity([self.request], changed, memory, pools)
        cpu = {"resource": "cpu", "requested": 4, "available": 2}
        for requests, changed, mem, expected in [
            (
                [self.request],
                dict(node, loadavg=["3"]),
                memory,
                [dict(cpu, requested=2, available=1)],
            ),
            (
                [self.request],
                dict(node, loadavg=["5"]),
                memory,
                [dict(cpu, requested=2, available=0)],
            ),
            ([self.request, self.request], node, memory, [cpu]),
            (
                [self.request],
                node,
                "MemAvailable: 1 kB\n",
                [{"resource": "memory", "requested": 4096, "available": 0}],
            ),
            (
                [self.request, self.request],
                node,
                "MemAvailable: 8388607 kB\n",
                [cpu, {"resource": "memory", "requested": 8192, "available": 8191}],
            ),
        ]:
            with self.subTest(node=changed, memory=mem, count=len(requests)):
                self.assertEqual(guest_host.check_capacity(requests, changed, mem, pools), expected)
        with self.assertRaises(guest_host.GuestError):
            guest_host.check_capacity([self.request], node, "MemFree: 8388608 kB\n", pools)
        with self.assertRaises(guest_host.ReasonError) as caught:
            guest_host.check_capacity(
                [self.request, self.request],
                node,
                "MemAvailable: 1 kB\n",
                {"pool": dict(storage, avail=32 * 1024**3)},
            )
        self.assertEqual(caught.exception.code, "storage-insufficient")
        for status in [{"extent_bytes": 0}, dict(storage, avail=None), dict(storage, avail=1.0)]:
            with self.subTest(status=status), self.assertRaises(guest_host.GuestError) as caught:
                guest_host.check_capacity([self.request], node, memory, {"pool": status})
            self.assertNotIsInstance(caught.exception, guest_host.ReasonError)

    def test_host_main_codes_only_reason_errors(self):
        envelope = {
            "requests": [],
            "mode": "plan",
            "confirmed": False,
            "exclusive": False,
            "level": "clean",
            "capture": False,
        }
        for error, expected in [
            (
                guest_host.ReasonError("vmid-in-use", "taken"),
                {"error": "taken", "code": "vmid-in-use"},
            ),
            (guest_host.GuestError("Invalid selected batch"), {"error": "Invalid selected batch"}),
        ]:
            stdout = io.StringIO()
            with (
                self.subTest(expected=expected),
                patch.object(guest_host, "read_line", return_value=envelope),
                patch.object(guest_host, "session", side_effect=error),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(guest_host.main(), 1)
            self.assertEqual(json.loads(stdout.getvalue()), expected)

    def read_error(self, event):
        reader, writer = os.pipe()
        os.write(writer, json.dumps(event).encode() + b"\n")
        os.close(writer)
        with (
            os.fdopen(reader, "rb", buffering=0) as stream,
            self.assertRaises(ValidationError) as caught,
        ):
            guests.read_event(SimpleNamespace(stdout=stream))
        return str(caught.exception)

    def test_host_reason_codes_map_to_controller_text(self):
        for code, text in guests.HOST_REASONS.items():
            with self.subTest(code=code):
                event = {"error": "HOST-TEXT", "code": code}
                self.assertEqual(self.read_error(event), "Native guest: " + text)
        generic = (
            "Native guest: operation failed; inspect ownership, configuration and prerequisites"
        )
        for event in [
            {"error": "HOST-TEXT"},
            {"error": "HOST-TEXT", "code": "unknown"},
            {"error": "HOST-TEXT", "code": "VMID-IN-USE"},
            {"error": "HOST-TEXT", "code": 1},
            {"error": "HOST-TEXT", "code": None},
            {"error": "HOST-TEXT", "code": ["vmid-in-use"]},
            {"error": "HOST-TEXT", "code": {"vmid-in-use": 1}},
            {"error": "HOST-TEXT", "code": "vmid-in-use", "detail": "HOST-TEXT"},
            {"error": 1, "code": "vmid-in-use"},
        ]:
            with self.subTest(event=event):
                self.assertEqual(self.read_error(event), generic)

    def test_host_reason_codes_match_controller_map(self):
        tree = ast.parse((ROOT / "scripts/guest_host.py").read_text())
        codes = {
            ast.literal_eval(node.args[1])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "refuse"
        }
        self.assertEqual(codes, set(guests.HOST_REASONS))

    def test_host_refusals_carry_reason_codes(self):
        foreign = [{"vmid": self.host["vmid"], "type": "lxc", "node": self.host["proxmox_node"]}]
        with self.assertRaises(guest_host.ReasonError) as caught:
            guest_host.resource_present(self.request, foreign)
        self.assertEqual(caught.exception.code, "vmid-in-use")

    def test_baseline_identity_binds_config_but_not_controller_revision(self):
        identity = guest_host.identity(self.request)
        other = copy.deepcopy(self.request)
        other["revision"] = "b" * 40
        self.assertEqual(guest_host.identity(other), identity)
        for key, value in [
            ("fqdn", "other.example.invalid"),
            ("memory_mib", 2048),
            ("cores", 3),
            ("ansible_user", "other"),
            ("vlan", 25),
        ]:
            other = copy.deepcopy(self.request)
            other["host"][key] = value
            self.assertNotEqual(guest_host.identity(other), identity)

    def test_guest_vlan_is_independent_of_verified_source(self):
        self.host["vlan"] = 25
        guest_host.validate_request(self.request)
        source = {
            "net0": "virtio=02:11:22:33:44:55,bridge=vmbr0,tag=30",
            "template": 1,
            "scsi0": "pool:base-9001-disk-0,size=4G",
        }
        template = dict(self.request["template"], vlan=30)
        source["description"] = (
            "kdive-template-v1:" + guest_host.template_host.identity(template) + ":ready"
        )
        resolved = guests.request_for(self.host, "a" * 40, source)
        self.assertEqual(resolved["template"], template)
        self.assertEqual(resolved["host"]["vlan"], 25)
        for field, value in [("net0", "virtio=bad,bridge=vmbr0,tag=0"), ("description", "foreign")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                guests.request_for(self.host, "a" * 40, dict(source, **{field: value}))

    def test_guest_overrides_and_balloon_default_are_explicit(self):
        self.host.update(cpu="x86-64-v3", nic_queues=4, bridge="vmbr2", storage="other-pool")
        guest_host.validate_request(self.request)
        config = guest_host.desired_config(self.request)
        self.assertEqual(config["cpu"], "x86-64-v3")
        self.assertEqual(config["balloon"], str(self.host["memory_mib"]))
        net = guest_host.desired_network(self.request, {"net0": "virtio=02:11:22:33:44:55"})
        self.assertEqual(net, "virtio=02:11:22:33:44:55,bridge=vmbr2,queues=4")
        baseline = guest_host.identity(self.request)
        for field, value in (
            ("cpu", "host"),
            ("nic_queues", 2),
            ("bridge", "vmbr3"),
            ("storage", "third-pool"),
            ("balloon_mib", 0),
        ):
            changed = copy.deepcopy(self.request)
            changed["host"][field] = value
            self.assertNotEqual(guest_host.identity(changed), baseline)
        self.host.update(balloon_mib=0, nic_model="e1000")
        self.host.pop("nic_queues")
        self.assertEqual(guest_host.desired_config(self.request)["balloon"], "0")
        self.assertTrue(
            guest_host.desired_network(
                self.request, {"net0": "virtio=02:11:22:33:44:55"}
            ).startswith("e1000=")
        )

    def test_guest_baseline_does_not_inherit_template_fixed_values(self):
        expected = guest_host.desired_config(self.request)
        with patch.dict(
            guest_host.template_host.FIXED,
            {"cpu": "other", "cores": "1", "memory": "128", "agent": "0"},
        ):
            self.assertEqual(guest_host.desired_config(self.request), expected)

    def test_disabled_balloon_preserves_legacy_identity(self):
        self.host.pop("cloudinit_snippet_storage")
        self.host["balloon_mib"] = 0
        expected = guest_host.template_host.digest(
            {
                "schema": 1,
                "management": 1,
                "template": guest_host.template_host.identity(self.request["template"]),
                "guest": {k: self.host[k] for k in guest_host.BASELINE_FIELDS}
                | {"vlan": self.host.get("vlan")},
            }
        )
        self.assertEqual(guest_host.identity(self.request), expected)
        self.host.pop("balloon_mib")
        self.assertNotEqual(guest_host.identity(self.request), expected)

    def test_snippet_storage_binds_only_ubuntu_identity(self):
        with_seed = guest_host.identity(self.request)
        legacy = copy.deepcopy(self.request)
        legacy["host"].pop("cloudinit_snippet_storage")
        expected = guest_host.template_host.digest(
            {
                "schema": 1,
                "management": 1,
                "template": guest_host.template_host.identity(self.request["template"]),
                "guest": {k: self.host[k] for k in guest_host.BASELINE_FIELDS}
                | {"vlan": self.host.get("vlan"), "balloon_mib": self.host["memory_mib"]},
            }
        )
        self.assertEqual(guest_host.identity(legacy), expected)
        self.assertNotEqual(with_seed, expected)
        legacy["host"]["cloudinit_snippet_storage"] = "other"
        self.assertNotIn(guest_host.identity(legacy), {with_seed, expected})

    def test_acknowledgement_cannot_mark_another_guest_ready(self):
        expected = {
            "vmid": self.host["vmid"],
            "identity": guest_host.identity(self.request),
            "verified": True,
            "phase": "prepared",
        }
        for wrong in [
            dict(expected, vmid=999),
            dict(expected, identity="b" * 64),
            dict(expected, verified=False),
            dict(expected, extra=1),
            dict(expected, phase="post-reboot"),
            {k: v for k, v in expected.items() if k != "phase"},
            {},
        ]:
            with self.subTest(wrong=wrong), self.assertRaises(guest_host.GuestError):
                guest_host.verify_ack(self.request, wrong)
        guest_host.verify_ack(self.request, expected)

    def test_native_configuration_rejects_drift_and_extra_disks(self):
        config = guest_host.desired_config(self.request)
        config.update(
            description=guest_host.marker(self.request, "ready"),
            net0="virtio=02:11:22:33:44:55,bridge=vmbr0",
            scsi0="pool:vm-1101-disk-0,size=32G",
            scsi1="pool:vm-1101-cloudinit,media=cdrom",
            efidisk0="pool:vm-1101-disk-1,efitype=4m,pre-enrolled-keys=1,size=4M",
        )
        config["ipconfig0"] = ",".join(reversed(config["ipconfig0"].split(",")))
        config["cicustom"] = "network=" + guest_host.seed_volume(self.request, config)[0]
        guest_host.check_configuration(self.request, config, [], "ready")
        for key, value in [
            ("cicustom", "network=local:snippets/other.yaml"),
            ("cicustom", config["cicustom"] + ",user=local:snippets/user.yaml"),
            ("onboot", 1),
            ("memory", 2),
            ("ide2", "foreign:disk"),
            ("description", "foreign"),
            ("template", 1),
            ("ciupgrade", 1),
        ]:
            with self.subTest(key=key), self.assertRaises(guest_host.GuestError):
                guest_host.check_configuration(
                    self.request, dict(config, **{key: value}), [], "ready"
                )
        with self.assertRaises(guest_host.GuestError):
            guest_host.check_configuration(
                self.request, config, [{"key": "cores", "pending": 3}], "ready"
            )

    def test_missing_seed_reference_is_drift(self):
        config = guest_host.desired_config(self.request)
        config.update(net0="virtio=02:11:22:33:44:55,bridge=vmbr0")
        for phase in ("preparing", "ready"):
            config["description"] = guest_host.marker(self.request, phase)
            with (
                self.subTest(phase=phase),
                self.assertRaisesRegex(guest_host.GuestError, "seed reference"),
            ):
                guest_host.check_configuration(self.request, config, [], phase)

    def test_network_seed_matches_mac_without_rename(self):
        seed = guest_host.network_seed(self.request, "BC:24:11:00:00:01")
        expected = (
            f"# kdive-guest-network-v1 identity={guest_host.identity(self.request)}\n"
            "version: 2\n"
            "ethernets:\n"
            "  kdive0:\n"
            "    match:\n"
            '      macaddress: "bc:24:11:00:00:01"\n'
            "    dhcp4: false\n"
            '    addresses: ["192.0.2.11/24"]\n'
            "    routes:\n"
            "      - to: default\n"
            '        via: "192.0.2.1"\n'
            "    nameservers:\n"
            '      addresses: ["192.0.2.53"]\n'
            '      search: ["example.invalid"]\n'
        )
        self.assertEqual(seed, expected.encode())
        self.assertNotIn(b"set-name", seed)
        volume, name, content = guest_host.seed_volume(
            self.request, {"net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0"}
        )
        self.assertEqual(content, seed)
        self.assertEqual(name, f"kdive-net-1101-{guest_host.hashlib.sha256(seed).hexdigest()}.yaml")
        self.assertEqual(volume, "local:snippets/" + name)
        other = guest_host.seed_volume(
            self.request, {"net0": "virtio=BC:24:11:00:00:02,bridge=vmbr0"}
        )
        self.assertNotEqual(other[1], name)

    def test_network_seed_canonicalizes_address(self):
        self.host["ipv4_cidr"] = "192.0.2.11/255.255.255.0"
        seed = guest_host.network_seed(self.request, "02:11:22:33:44:55")
        self.assertIn(b'addresses: ["192.0.2.11/24"]', seed)


class TestGuestVerifier(unittest.TestCase):
    def test_cloud_init_accepts_only_the_native_user_deprecation(self):
        notice = (
            "'user' of type string is deprecated in 22.2 and scheduled to be removed in 27.2. "
            "Use 'users' list instead."
        )
        clean = {"status": "done", "errors": [], "recoverable_errors": {}}
        advisory = dict(clean, recoverable_errors={"DEPRECATED": [notice, notice]})
        for code, state in [(0, clean), (0, advisory), (2, advisory)]:
            with (
                self.subTest(code=code, state=state),
                patch.object(
                    guest_verify.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, json.dumps(state), ""),
                ),
            ):
                guest_verify.cloud_init_ready()
        for code, state in [
            (1, advisory),
            (2, clean),
            (3, clean),
            (2, dict(advisory, status="running")),
            (2, dict(advisory, errors=["failed module"])),
            (0, dict(clean, errors=["failed module"])),
            (2, dict(advisory, recoverable_errors={"DEPRECATED": [notice, "other"]})),
            (2, dict(advisory, recoverable_errors={"DEPRECATED": [notice], "WARNING": ["other"]})),
            (2, dict(advisory, recoverable_errors={"DEPRECATED": notice})),
            (2, dict(advisory, recoverable_errors={"DEPRECATED": []})),
            (0, dict(clean, errors="")),
            (0, dict(clean, recoverable_errors=[])),
        ]:
            with (
                self.subTest(code=code, state=state),
                patch.object(
                    guest_verify.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, json.dumps(state), ""),
                ),
                self.assertRaises(guest_verify.GuestError),
            ):
                guest_verify.cloud_init_ready()

    def test_wrong_peer_identity_is_rejected_before_preparation(self):
        req = request()
        req["guest_uuid"] = "12345678-1234-1234-1234-123456789abc"
        for wrong in ("fqdn", "release", "uuid", "missing_uuid", "malformed_uuid"):
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = {
                    "/etc/os-release": (
                        f'ID=ubuntu\nVERSION_ID="{req["template"]["image"]["release"]}"\n'
                    ),
                    "/proc/cpuinfo": "flags : vmx",
                    "/proc/meminfo": "MemTotal: 4194304 kB\n",
                    "/sys/module/apparmor/parameters/enabled": "Y",
                    "/sys/class/dmi/id/product_uuid": req["guest_uuid"].upper() + "\n",
                }
                if wrong == "release":
                    files["/etc/os-release"] = 'ID=ubuntu\nVERSION_ID="22.04"\n'
                if wrong in {"uuid", "malformed_uuid"}:
                    files["/sys/class/dmi/id/product_uuid"] = (
                        "87654321-1234-1234-1234-123456789abc" if wrong == "uuid" else "bad"
                    )
                if wrong == "missing_uuid":
                    del files["/sys/class/dmi/id/product_uuid"]
                for path, value in files.items():
                    target = root / path.lstrip("/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(value)
                module = root / "etc/modules-load.d/kdive-kvm.conf"
                module.parent.mkdir(parents=True)
                calls = []

                def command(argv, calls=calls, **kwargs):
                    calls.append(argv)
                    if argv[0] == "cloud-init":
                        output = json.dumps(
                            {
                                "status": "done",
                                "errors": [],
                                "recoverable_errors": {
                                    "DEPRECATED": [
                                        "'user' of type string is deprecated in 22.2 and scheduled "
                                        "to be removed in 27.2. Use 'users' list instead."
                                    ]
                                },
                            }
                        )
                    elif argv[0] == "ip":
                        output = json.dumps(
                            [
                                {
                                    "addr_info": [
                                        {"family": "inet", "local": req["host"]["ansible_host"]}
                                    ]
                                }
                            ]
                        )
                    elif argv[0] == "aa-status":
                        output = '{"profiles":{"example":"enforce"}}'
                    else:
                        output = "1"
                    return subprocess.CompletedProcess(
                        argv, 2 if argv[0] == "cloud-init" else 0, output, ""
                    )

                with (
                    patch.object(
                        guest_verify, "Path", side_effect=lambda p, root=root: root / p.lstrip("/")
                    ),
                    patch.object(guest_verify.os, "geteuid", return_value=0),
                    patch.object(guest_verify.os, "cpu_count", return_value=2),
                    patch.object(
                        guest_verify.os,
                        "statvfs",
                        return_value=SimpleNamespace(
                            f_blocks=31 * 1024**3,
                            f_frsize=1,
                        ),
                    ),
                    patch.object(guest_verify.platform, "system", return_value="Linux"),
                    patch.object(guest_verify.platform, "machine", return_value="x86_64"),
                    patch.object(guest_verify.socket, "gethostname", return_value="ubuntu"),
                    patch.object(
                        guest_verify.socket,
                        "getfqdn",
                        return_value=(
                            "other.example.invalid" if wrong == "fqdn" else req["host"]["fqdn"]
                        ),
                    ),
                    patch.object(guest_verify.shutil, "which", return_value="/tool"),
                    patch.object(guest_verify.subprocess, "run", side_effect=command),
                    patch.object(guest_verify.os, "open", return_value=10),
                    patch.object(guest_verify.os, "close"),
                    patch.object(guest_verify.fcntl, "ioctl", side_effect=[12, 11]),
                ):
                    message = (
                        "Guest hostname/FQDN differs"
                        if wrong == "fqdn"
                        else (
                            "Guest OS/release/architecture differs"
                            if wrong == "release"
                            else "Guest UUID"
                        )
                    )
                    with self.assertRaisesRegex(guest_verify.GuestError, message):
                        guest_verify.run(req, fresh=True)
                self.assertFalse(any(c[0] in {"apt-get", "systemctl", "modprobe"} for c in calls))
                self.assertFalse(module.exists())

    def test_preparation_installs_only_missing_agent_and_persists_existing_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cpu = root / "cpuinfo"
            cpu.write_text("flags : vmx\n")
            module = root / "module.conf"
            original_path = Path

            def path(value):
                return {"/proc/cpuinfo": cpu, "/etc/modules-load.d/kdive-kvm.conf": module}.get(
                    value, original_path(value)
                )

            def available(tool):
                return None if tool == "qemu-ga" else "/tool"

            with (
                patch.object(guest_verify, "Path", side_effect=path),
                patch.object(guest_verify.shutil, "which", side_effect=available),
                patch.object(
                    guest_verify.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as run,
            ):
                guest_verify.prepare("ubuntu")
            self.assertEqual(
                [c.args[0] for c in run.call_args_list],
                [
                    ["apt-get", "update"],
                    ["apt-get", "install", "-y", "--no-install-recommends", "qemu-guest-agent"],
                    ["systemctl", "enable", "--now", "qemu-guest-agent"],
                    ["modprobe", "kvm_intel"],
                ],
            )
            self.assertEqual(module.read_text(), "kvm_intel\n")
            self.assertEqual(module.stat().st_mode & 0o777, 0o644)
            with (
                patch.object(guest_verify.shutil, "which", side_effect=available),
                patch.object(guest_verify.subprocess, "run") as run,
            ):
                with self.assertRaises(guest_verify.GuestError):
                    guest_verify.prepare("fedora")
                run.assert_not_called()

    def test_security_checks_actual_enforcement(self):
        with patch.object(guest_verify.Path, "read_text", return_value="0"):
            with self.assertRaises(guest_verify.GuestError):
                guest_verify.security_state("rocky")
        with patch.object(guest_verify.Path, "read_text", side_effect=["1", "none [integrity]"]):
            self.assertEqual(guest_verify.security_state("opensuse"), "selinux-enforcing")
        for value in ("[none] integrity", "none integrity [confidentiality]", ""):
            with patch.object(guest_verify.Path, "read_text", side_effect=["1", value]):
                with self.assertRaises(guest_verify.GuestError):
                    guest_verify.security_state("opensuse")
        with (
            patch.object(guest_verify.Path, "read_text", return_value="Y"),
            patch.object(
                guest_verify.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, '{"profiles":{"example":"complain"}}', ""
                ),
            ),
        ):
            with self.assertRaises(guest_verify.GuestError):
                guest_verify.security_state("ubuntu")

    def test_observation_rejects_bad_os_name_size_and_security(self):
        req = request()
        req["guest_uuid"] = "12345678-1234-1234-1234-123456789abc"
        observed = {
            "guest_uuid": req["guest_uuid"].upper(),
            "boot_id": "12345678-1234-1234-1234-123456789abd",
            "os_id": "ubuntu",
            "release": req["template"]["image"]["release"],
            "architecture": "x86_64",
            "hostname": "ubuntu",
            "fqdn": req["host"]["fqdn"],
            "ipv4": [req["host"]["ansible_host"]],
            "cpus": 2,
            "memory_bytes": 4 * 1024**3,
            "crash_reserved_bytes": 0,
            "balloon_driver": True,
            "filesystem_bytes": 31 * 1024**3,
            "security": "apparmor-enforcing",
            "kvm_api": 12,
            "kvm_create_vm": True,
        }
        guest_verify.validate_observation(req, observed)
        cases = {
            "boot_id": "invalid",
            "guest_uuid": "87654321-1234-1234-1234-123456789abc",
            "os_id": "debian",
            "release": "22.04",
            "architecture": "aarch64",
            "hostname": "wrong",
            "fqdn": "wrong.example.invalid",
            "ipv4": [],
            "cpus": 1,
            "memory_bytes": 1024**3,
            "filesystem_bytes": 4 * 1024**3,
            "security": "disabled",
            "kvm_api": 11,
            "kvm_create_vm": False,
        }
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaises(guest_verify.GuestError):
                guest_verify.validate_observation(req, dict(observed, **{field: value}))

        req["host"]["balloon_mib"] = 1024
        guest_verify.validate_observation(req, dict(observed, memory_bytes=1024**3))
        for bad in (
            dict(observed, balloon_driver=False),
            dict(observed, memory_bytes=512 * 1024**2),
        ):
            with self.assertRaises(guest_verify.GuestError):
                guest_verify.validate_observation(req, bad)
        req["host"]["balloon_mib"] = 0
        guest_verify.validate_observation(req, dict(observed, balloon_driver=False))
        req["host"].pop("balloon_mib")
        for configured_mib, usable, reserved in (
            (4096, 3819302912, 256 * 1024**2),
            (4096, 4 * 1024**3, 0),
            (4096, 3584 * 1024**2, 512 * 1024**2),
            (512, 384 * 1024**2, 128 * 1024**2),
        ):
            req["host"]["memory_mib"] = configured_mib
            with self.subTest(configured_mib=configured_mib, reserved=reserved):
                guest_verify.validate_observation(
                    req, dict(observed, memory_bytes=usable, crash_reserved_bytes=reserved)
                )
        for configured_mib, usable, reserved in (
            (4096, 3 * 1024**3, 256 * 1024**2),
            (4096, 4 * 1024**3, -1),
            (4096, 4 * 1024**3, True),
            (4096, 4 * 1024**3, "0"),
            (4096, 4 * 1024**3, None),
            (4096, 4 * 1024**3, 512 * 1024**2 + 1),
            (512, 512 * 1024**2, 128 * 1024**2 + 1),
            (4096, 4 * 1024**3, 2**64),
            (4096, 4 * 1024**3 + 1, 0),
            (4096, 4 * 1024**3, 1),
            (4096, True, 0),
            (4096, -1, 0),
        ):
            req["host"]["memory_mib"] = configured_mib
            with self.subTest(usable=usable, reserved=reserved):
                with self.assertRaises(guest_verify.GuestError):
                    guest_verify.validate_observation(
                        req, dict(observed, memory_bytes=usable, crash_reserved_bytes=reserved)
                    )
        with self.assertRaises(guest_verify.GuestError):
            guest_verify.validate_observation(
                req,
                {key: value for key, value in observed.items() if key != "crash_reserved_bytes"},
            )

    def test_crash_reservation_cap_is_one_gib_for_ubuntu_only(self):
        req = request()
        req["guest_uuid"] = "12345678-1234-1234-1234-123456789abc"
        req["host"].pop("balloon_mib", None)
        observed = {
            "guest_uuid": req["guest_uuid"],
            "boot_id": "12345678-1234-1234-1234-123456789abd",
            "os_id": "ubuntu",
            "release": req["template"]["image"]["release"],
            "architecture": "x86_64",
            "hostname": "ubuntu",
            "fqdn": req["host"]["fqdn"],
            "ipv4": [req["host"]["ansible_host"]],
            "cpus": 2,
            "balloon_driver": True,
            "filesystem_bytes": 31 * 1024**3,
            "security": "apparmor-enforcing",
            "kvm_api": 12,
            "kvm_create_vm": True,
        }

        def check(memory_mib, usable, reserved):
            req["host"]["memory_mib"] = memory_mib
            guest_verify.validate_observation(
                req, dict(observed, memory_bytes=usable, crash_reserved_bytes=reserved)
            )

        # Ubuntu kdump-tools reserves 1 GiB in its 32G-64G range.
        check(32768, 31805460 * 1024, 1024**3)
        for memory_mib, usable, reserved in (
            (32768, 31 * 1024**3 - 1, 1024**3 + 1),
            (2048, 1536 * 1024**2 - 1, 512 * 1024**2 + 1),
        ):
            with (
                self.subTest(memory_mib=memory_mib, reserved=reserved),
                self.assertRaisesRegex(guest_verify.GuestError, "memory evidence invalid"),
            ):
                check(memory_mib, usable, reserved)
        req["host"]["profile"] = "fedora"
        observed.update(os_id="fedora", hostname="ubuntu", security="selinux-enforcing")
        req["template"]["image"]["release"] = observed["release"]
        check(32768, 31 * 1024**3 + 512 * 1024**2, 512 * 1024**2)
        for reserved in (512 * 1024**2 + 1, 1024**3):
            with (
                self.subTest(profile="fedora", reserved=reserved),
                self.assertRaisesRegex(guest_verify.GuestError, "memory evidence invalid"),
            ):
                check(32768, 31 * 1024**3, reserved)

    def test_crash_reservation_uses_native_sysfs_bytes(self):
        for native, expected in (("0\n", 0), ("268435456\n", 268435456)):
            with patch.object(guest_verify.Path, "read_text", return_value=native):
                self.assertEqual(guest_verify.crash_reservation(), expected)
        with patch.object(guest_verify.Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(guest_verify.crash_reservation(), 0)
        for native in ("", "-1", "1.0", "True", "0x100", "1 2", "1" * 21):
            with patch.object(guest_verify.Path, "read_text", return_value=native):
                with self.assertRaises(guest_verify.GuestError):
                    guest_verify.crash_reservation()
        with patch.object(guest_verify.Path, "read_text", side_effect=PermissionError):
            with self.assertRaises(OSError):
                guest_verify.crash_reservation()

    def test_crash_reservation_waits_for_kexec_lock(self):
        with (
            patch.object(
                guest_verify.Path,
                "read_text",
                side_effect=[OSError(errno.EBUSY, "busy"), "268435456\n"],
            ) as read,
            patch.object(guest_verify.time, "monotonic", return_value=0),
            patch.object(guest_verify.time, "sleep"),
        ):
            self.assertEqual(guest_verify.crash_reservation(), 268435456)
            self.assertEqual(read.call_count, 2)

    def test_crash_reservation_busy_deadline_and_other_errors(self):
        with (
            patch.object(guest_verify.Path, "read_text", side_effect=OSError(errno.EBUSY, "busy")),
            patch.object(guest_verify.time, "monotonic", side_effect=[0, 30]),
            patch.object(guest_verify.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(guest_verify.GuestError, "reservation.*timed out"):
                guest_verify.crash_reservation()
            sleep.assert_not_called()
        for code in (errno.EIO, errno.EACCES):
            with (
                self.subTest(code=code),
                patch.object(guest_verify.Path, "read_text", side_effect=OSError(code, "failed")),
                patch.object(guest_verify.time, "sleep") as sleep,
            ):
                with self.assertRaises(OSError) as raised:
                    guest_verify.crash_reservation()
                self.assertEqual(raised.exception.errno, code)
                sleep.assert_not_called()

    def test_kvm_descriptors_close_on_success_and_failure(self):
        with (
            patch.object(guest_verify.os, "open", return_value=11),
            patch.object(guest_verify.os, "close") as close,
            patch.object(guest_verify.fcntl, "ioctl", side_effect=[12, 12]),
        ):
            self.assertEqual(guest_verify.kvm_probe(), 12)
            self.assertEqual([call.args[0] for call in close.call_args_list], [12, 11])
        with (
            patch.object(guest_verify.os, "open", return_value=11),
            patch.object(guest_verify.os, "close") as close,
            patch.object(guest_verify.fcntl, "ioctl", side_effect=[12, OSError()]),
        ):
            with self.assertRaises(guest_verify.GuestError):
                guest_verify.kvm_probe()
            close.assert_called_once_with(11)


class TestOpenSusePrerequisite(unittest.TestCase):
    def summary(self):
        # Sanitized action/identity fields captured from native local-RPM dry-run XML.
        return (
            '<install-summary packages-to-change="3" need-reboot="1">'
            '<to-install><solvable type="package" name="kernel-default" '
            'edition="6.12.0-160000.38.1" arch="x86_64" repository="_tmpRPMcache_"/>'
            '<solvable type="package" name="ucode-intel" '
            'edition="20260812-160000.1.1" arch="x86_64" repository="_tmpRPMcache_"/>'
            '</to-install><to-remove><solvable type="package" name="kernel-default-base" '
            'edition="6.12.0-160000.38.1.160000.2.24" arch="x86_64" repository="@System"/>'
            "</to-remove></install-summary>"
        )

    def test_actual_transaction_confirms_only_exact_plan(self):
        self.assertTrue(callable(getattr(guest_verify, "confirm_kernel_transaction", None)))
        import sys

        body = self.summary() + '<prompt id="0"><text>Continue?</text></prompt>'
        script = (
            "import sys,termios;tty=open('/dev/tty');"
            "assert not termios.tcgetattr(tty)[3] & termios.ECHO;"
            "print('<stream>' + " + repr(body) + ",flush=True);"
            "answer=tty.readline();print('</stream>',flush=True);"
            "sys.exit(0 if answer == 'y\\n' else 9)"
        )
        try:
            guest_verify.confirm_kernel_transaction([sys.executable, "-c", script], timeout=5)
        except guest_verify.GuestError as error:
            self.fail(str(error))

    def test_transaction_rejects_changed_actions_and_prompts(self):
        self.assertTrue(callable(getattr(guest_verify, "confirm_kernel_transaction", None)))
        import sys

        summary = self.summary()
        prompt = '<prompt id="0"><text>Continue?</text></prompt>'
        bad = [
            summary.replace("20260812-160000.1.1", "20260812-160000.1.2") + prompt,
            summary.replace('arch="x86_64"', 'arch="aarch64"', 1) + prompt,
            summary.replace('packages-to-change="3"', 'packages-to-change="4"') + prompt,
            summary.replace("<to-install>", '<to-install><solvable name="extra"/>') + prompt,
            summary.replace("</install-summary>", "<to-upgrade/></install-summary>") + prompt,
            summary.replace('repository="_tmpRPMcache_"', 'repository="untrusted"') + prompt,
            prompt + summary,
            summary + prompt.replace('id="0"', 'id="1"'),
            summary + prompt + prompt,
            '<message type="error">failed</message>' + summary + prompt,
            "<malformed>" + summary + prompt,
        ]
        for body in bad:
            script = "import sys;print(" + repr("<stream>" + body + "</stream>") + ",flush=True)"
            with self.subTest(body=body), self.assertRaises(guest_verify.GuestError):
                guest_verify.confirm_kernel_transaction([sys.executable, "-c", script], timeout=5)

    def exercise_prerequisite(self, fault=None):
        self.assertTrue(callable(getattr(guest_verify, "prepare_opensuse_kernel", None)))
        import sys

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kernel = root / "vmlinuz"
            kernel.write_bytes(b"pinned kernel")
            installed = root / "installed"
            calls = []
            real_path, popen = Path, subprocess.Popen
            summary = self.summary()

            def native(argv, **kwargs):
                calls.append(argv)
                output = ""
                code = 0
                if argv[0] == "zypper" and "repos" in argv:
                    output = (
                        '<stream><repo-list><repo alias="openSUSE:repo-oss" '
                        'enabled="1" gpgcheck="1" repo_gpgcheck="1"/>'
                        "</repo-list></stream>"
                    )
                elif argv[0] == "zypper" and "download" in argv:
                    cache = Path(argv[argv.index("--pkg-cache-dir") + 1])
                    for name, version in guest_verify.OPENSUSE_PACKAGES.items():
                        (cache / f"{name}-{version}.x86_64.rpm").write_bytes(b"signed rpm")
                elif argv[:2] == ["rpm", "-qp"]:
                    filename = Path(argv[-1]).name
                    if "%{RSAHEADER:pgpsig}" in argv:
                        output = "RSA/SHA256, signature"
                    else:
                        name = next(
                            n
                            for n in guest_verify.OPENSUSE_PACKAGES
                            if filename.startswith(n + "-")
                        )
                        output = f"{name}|{guest_verify.OPENSUSE_PACKAGES[name]}|x86_64"
                elif argv[:2] == ["rpm", "--checksig"]:
                    output = "digests signatures OK"
                elif argv[:2] == ["rpm", "-q"]:
                    if argv[-1] == "kernel-default-base":
                        output = "kernel-default-base|6.12.0-160000.38.1.160000.2.24|x86_64"
                    else:
                        output = "0:" + guest_verify.OPENSUSE_PACKAGES[argv[-1]]
                        if fault == "version":
                            output += ".1"
                elif argv[:2] == ["rpm", "-qa"]:
                    output = (
                        "kernel-default\nucode-intel\n"
                        if installed.exists()
                        else "kernel-default-base\n"
                    )
                else:
                    raise AssertionError(argv)
                return subprocess.CompletedProcess(argv, code, output, "")

            def transaction(argv, **kwargs):
                calls.append(argv)
                self.assertNotIn("--non-interactive", argv)
                self.assertEqual(argv[-1], "-kernel-default-base")
                script = (
                    "import pathlib,sys;print("
                    + repr("<stream>" + summary + '<prompt id="0"/>')
                    + ",flush=True);"
                    "answer=open('/dev/tty').readline();assert answer == 'y\\n';"
                    "pathlib.Path(" + repr(str(installed)) + ").write_text('yes');"
                    "print('</stream>',flush=True)"
                )
                if fault == "install":
                    script = script.replace(".write_text('yes')", ".write_text('yes');sys.exit(9)")
                if fault == "binary":
                    script += ";pathlib.Path(" + repr(str(kernel)) + ").write_bytes(b'changed')"
                return popen(argv[:4] + [sys.executable, "-c", script], **kwargs)

            def path(value):
                if str(value).startswith("/boot/vmlinuz-"):
                    return kernel
                if str(value) == "/sys/fs/selinux/enforce":
                    target = root / "selinux"
                    target.write_text("1")
                    return target
                if str(value) == "/sys/kernel/security/lockdown":
                    target = root / "lockdown"
                    target.write_text(
                        "[none] integrity"
                        if fault == "security" and installed.exists()
                        else "none [integrity] confidentiality"
                    )
                    return target
                return real_path(value)

            with (
                patch.object(guest_verify, "Path", side_effect=path),
                patch.object(
                    guest_verify.platform, "release", return_value="6.12.0-160000.38-default"
                ),
                patch.object(
                    guest_verify.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**3)
                ),
                patch.object(guest_verify.subprocess, "run", side_effect=native),
                patch.object(guest_verify.subprocess, "Popen", side_effect=transaction),
            ):
                if fault:
                    with self.assertRaises(guest_verify.GuestError):
                        guest_verify.prepare_opensuse_kernel()
                else:
                    guest_verify.prepare_opensuse_kernel()
            self.assertTrue(installed.exists())
            self.assertEqual(
                kernel.read_bytes(), b"changed" if fault == "binary" else b"pinned kernel"
            )
            self.assertEqual(sum(c[:2] == ["rpm", "--checksig"] for c in calls), 2)

    def test_signed_prerequisite_installs_exact_packages_and_preserves_kernel(self):
        self.exercise_prerequisite()

    def test_failed_transaction_or_changed_postconditions_never_continue(self):
        for fault in ("install", "binary", "version", "security"):
            with self.subTest(fault=fault):
                self.exercise_prerequisite(fault)

    def test_package_download_fails_before_install_on_trust_or_identity_drift(self):
        for fault in (
            "repository",
            "download",
            "missing",
            "symlink",
            "header",
            "unsigned",
            "signature",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:

                def native(argv, fault=fault, **kwargs):
                    output, code = "", 0
                    if "repos" in argv:
                        output = (
                            '<stream><repo alias="openSUSE:repo-oss" enabled="1" '
                            'gpgcheck="1" repo_gpgcheck="'
                            + ("0" if fault == "repository" else "1")
                            + '"/></stream>'
                        )
                    elif "download" in argv:
                        code = 1 if fault == "download" else 0
                        for name, version in guest_verify.OPENSUSE_PACKAGES.items():
                            file = Path(directory) / f"{name}-{version}.x86_64.rpm"
                            if fault == "missing":
                                break
                            if fault == "symlink":
                                file.symlink_to("/nonexistent")
                            else:
                                file.write_bytes(b"rpm")
                    elif argv[:2] == ["rpm", "--checksig"]:
                        code = 1 if fault == "signature" else 0
                    elif "%{RSAHEADER:pgpsig}" in argv:
                        output = "(none)" if fault == "unsigned" else "RSA/SHA256, signature"
                    else:
                        name = next(
                            n
                            for n in guest_verify.OPENSUSE_PACKAGES
                            if Path(argv[-1]).name.startswith(n + "-")
                        )
                        output = f"{name}|{guest_verify.OPENSUSE_PACKAGES[name]}|x86_64"
                        if fault == "header":
                            output += "extra"
                    return subprocess.CompletedProcess(argv, code, output, "")

                with (
                    patch.object(guest_verify.subprocess, "run", side_effect=native),
                    self.assertRaises(guest_verify.GuestError),
                ):
                    guest_verify.download_kernel_packages(directory)

    def test_prerequisite_rejects_kernel_variant_and_space_drift(self):
        for fault in ("kernel", "base", "space"):
            calls = []

            def native(argv, fault=fault, calls=calls, **kwargs):
                calls.append(argv)
                output = (
                    "wrong"
                    if fault == "base"
                    else "kernel-default-base|6.12.0-160000.38.1.160000.2.24|x86_64"
                )
                return subprocess.CompletedProcess(argv, 0, output, "")

            with (
                self.subTest(fault=fault),
                patch.object(
                    guest_verify.platform,
                    "release",
                    return_value=("wrong" if fault == "kernel" else guest_verify.OPENSUSE_KERNEL),
                ),
                patch.object(guest_verify.subprocess, "run", side_effect=native),
                patch.object(guest_verify.Path, "read_text", side_effect=["1", "[integrity]"]),
                patch.object(
                    guest_verify.shutil, "disk_usage", return_value=SimpleNamespace(free=1)
                ),
                self.assertRaises(guest_verify.GuestError),
            ):
                guest_verify.prepare_opensuse_kernel()
            self.assertFalse(any(c[0] == "zypper" for c in calls))

    def test_transaction_timeout_and_output_bound(self):
        self.assertTrue(callable(getattr(guest_verify, "confirm_kernel_transaction", None)))
        import sys

        for script in ("import time;time.sleep(10)", "print('x' * 1048577)"):
            with self.subTest(script=script), self.assertRaises(guest_verify.GuestError):
                guest_verify.confirm_kernel_transaction([sys.executable, "-c", script], timeout=0.1)


class TestGuestBoot(unittest.TestCase):
    def test_reboot_requires_same_boot_before_one_graceful_request(self):
        self.assertTrue(callable(getattr(guest_verify, "reboot", None)))
        req = request()
        req["host"]["profile"] = "opensuse"
        req["template"]["image"]["release"] = "16.0"
        req["guest_uuid"] = "12345678-1234-1234-1234-123456789abc"
        observed = dict(
            guest_uuid=req["guest_uuid"],
            os_id="opensuse-leap",
            release="16.0",
            architecture="x86_64",
            hostname=req["host"]["fqdn"].split(".")[0],
            fqdn=req["host"]["fqdn"],
            ipv4=[req["host"]["ansible_host"]],
        )
        boot = "87654321-1234-1234-1234-123456789abc"
        with (
            patch.object(guest_verify.os, "geteuid", return_value=0),
            patch.object(guest_verify.platform, "system", return_value="Linux"),
            patch.object(guest_verify, "identity_observation", return_value=observed),
            patch.object(guest_verify.Path, "read_text", return_value=boot),
            patch.object(
                guest_verify.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run,
        ):
            guest_verify.reboot(req, boot)
            self.assertEqual(run.call_args.args[0], ["systemctl", "--no-block", "reboot"])
            self.assertEqual(run.call_count, 1)
            run.reset_mock()
            for old in (None, "bad", req["guest_uuid"]):
                with self.subTest(old=old), self.assertRaises(guest_verify.GuestError):
                    guest_verify.reboot(req, old)
            self.assertEqual(run.call_count, 0)
            observed["guest_uuid"] = boot
            with self.assertRaises(guest_verify.GuestError):
                guest_verify.reboot(req, boot)
            self.assertEqual(run.call_count, 0)

    def test_reconnect_rejects_unchanged_or_invalid_boot_without_rebooting(self):
        req = request()
        old = "12345678-1234-1234-1234-123456789abd"
        for value in (old, "bad"):
            with (
                self.subTest(value=value),
                patch.object(guests, "guest_ssh", return_value=["ssh"]),
                patch.object(
                    guests,
                    "run_guest",
                    return_value=subprocess.CompletedProcess([], 0, value, ""),
                ) as run,
                patch.object(guests.time, "monotonic", side_effect=[0, 601]),
                self.assertRaises(ValueError),
            ):
                guests.wait_guest(req, Path("unused"), False, old)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], "cat /proc/sys/kernel/random/boot_id")


class GuestNativeFixture:
    """Native command boundary with persistent template/guest configuration and disks."""

    def __init__(self, req):
        self.template = NativeFixture(req["template"])
        self.request = req
        self.guests = {}
        self.volumes = []
        self.calls = []
        self.fail_clone = False
        self.destination_extent = 0
        self.snapshots = {}
        self.disk_state = {}
        self.snippets = None
        self.nodes = None
        self.snippet_status = {"active": 1, "enabled": 1, "type": "dir", "content": "iso,snippets"}

    def __call__(self, argv, **kwargs):
        result = self.run(argv, **kwargs)
        if self.nodes is not None:
            self.sync_configuration_files()
        return result

    def sync_configuration_files(self):
        """Mirror native configuration files: current, pending and snapshot sections."""
        directory = self.nodes / self.template.node / "qemu-server"
        directory.mkdir(parents=True, exist_ok=True)
        for path in directory.glob("*.conf"):
            path.unlink()
        configs = {9001: {"config": self.template.config}} if self.template.config else {}
        configs |= self.guests
        for vmid, guest in configs.items():
            sections = [guest["config"], guest.get("pending", [])]
            sections += [snap["config"] for snap in self.snapshots.get(vmid, {}).values()]
            (directory / f"{vmid}.conf").write_text(json.dumps(sections))

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["pvesm", "path"]:
            return str(self.snippets / argv[2].rsplit("/", 1)[1]) + "\n"
        if argv[:2] in (
            ["qm", "snapshot"],
            ["qm", "rollback"],
            ["qm", "shutdown"],
            ["qm", "destroy"],
        ):
            vmid = int(argv[2])
            guest = self.guests[vmid]
            if argv[1] == "shutdown":
                guest["status"] = "stopped"
            elif argv[1] == "snapshot":
                self.snapshots.setdefault(vmid, {})[argv[3]] = {
                    "config": {
                        k: v
                        for k, v in guest["config"].items()
                        if k not in {"digest", "description"}
                    },
                    **(
                        {"parent": guest["config"]["parent"]} if "parent" in guest["config"] else {}
                    ),
                    "description": argv[argv.index("--description") + 1],
                    "snaptime": 123456,
                    "vmstate": 0,
                    "disk_state": copy.deepcopy(self.disk_state.get(vmid, {})),
                }
                guest["config"]["parent"] = argv[3]
            elif argv[1] == "rollback":
                snap = self.snapshots[vmid][argv[3]]
                guest["config"] = copy.deepcopy(snap["config"]) | {
                    "description": guest["config"]["description"],
                    "digest": "a" * 40,
                    "parent": argv[3],
                    "vmgenid": "12345678-1234-1234-1234-123456789abc",
                }
                self.disk_state[vmid] = copy.deepcopy(snap["disk_state"])
            else:
                del self.guests[vmid]
                self.snapshots.pop(vmid, None)
                self.volumes = [v for v in self.volumes if v["vmid"] != vmid]
            return ""
        if argv[0] == "vgs":
            return self.template(argv, **kwargs)
        if argv[0] == "pvesh":
            return self.read(argv)
        if argv[:2] == ["qm", "clone"]:
            if self.fail_clone:
                raise guest_host.GuestError("Native command failed; inspect host task state")
            vmid = int(argv[3])
            opts = dict(zip(argv[4::2], argv[5::2], strict=True))
            config = copy.deepcopy(self.template.config)
            config.pop("template")
            config.update(name=opts["--name"], description=opts["--description"])
            config["net0"] = (
                f"virtio=02:00:00:00:{vmid // 256:02x}:{vmid % 256:02x},"
                + config["net0"].split(",", 1)[1]
            )
            config["digest"] = "a" * 40
            config["smbios1"] = f"uuid=00000000-0000-4000-8000-{vmid:012d}"
            for slot in ("scsi0", "ide2", "efidisk0"):
                config[slot] = (
                    config[slot]
                    .replace("base-9001", f"vm-{vmid}")
                    .replace("vm-9001", f"vm-{vmid}")
                    .replace("pool:", opts["--storage"] + ":")
                )
            self.guests[vmid] = {"config": config, "status": "stopped", "pending": []}
            for v in self.template.volumes:
                self.volumes.append(
                    dict(
                        v,
                        vmid=vmid,
                        size=guest_host.template_host.allocated_size(
                            v["size"], self.destination_extent
                        ),
                        volid=v["volid"]
                        .replace("base-9001", f"vm-{vmid}")
                        .replace("vm-9001", f"vm-{vmid}")
                        .replace("pool:", opts["--storage"] + ":"),
                    )
                )
            return ""
        vmid = int(argv[2])
        if vmid == 9001:
            return self.template(argv, **kwargs)
        guest = self.guests[vmid]
        if argv[:2] == ["qm", "set"]:
            opts = dict(zip(argv[3::2], argv[4::2], strict=True))
            if "--digest" in opts:
                if opts.pop("--digest") != guest["config"]["digest"]:
                    raise guest_host.GuestError("Native configuration changed")
            if "--delete" in opts:
                slot = opts.pop("--delete")
                volume = guest["config"].pop(slot).split(",")[0]
                self.volumes = [v for v in self.volumes if v["volid"] != volume]
            if opts.get("--scsi1", "").endswith(":cloudinit"):
                storage = opts["--scsi1"].split(":")[0]
                volume = f"{storage}:vm-{vmid}-cloudinit"
                opts["--scsi1"] = volume + ",media=cdrom,size=4M"
                self.volumes.append(
                    dict(
                        vmid=vmid,
                        volid=volume,
                        size=guest_host.template_host.allocated_size(
                            4 * 1024**2, self.destination_extent
                        ),
                        format="raw",
                        content="images",
                    )
                )
            for key, value in opts.items():
                guest["config"][key.removeprefix("--")] = (
                    Path(value).read_text() if key == "--sshkeys" else value
                )
        elif argv[:2] == ["qm", "resize"]:
            size = int(argv[4].removesuffix("G")) * 1024**3
            for volume in self.volumes:
                if volume["vmid"] == vmid and volume["volid"].endswith("disk-0"):
                    volume["size"] = size
        elif argv[:2] == ["qm", "start"]:
            guest["status"] = "running"
        elif argv[:2] != ["qm", "agent"]:
            raise AssertionError(argv)
        return ""

    def read(self, argv):
        path = argv[2]
        if path == "/cluster/resources":
            return json.dumps(
                [
                    {"vmid": vmid, "type": "qemu", "node": self.template.node}
                    for vmid in [9001, *self.guests]
                ]
            )
        if path.endswith("/content"):
            return json.dumps(self.template.volumes + self.volumes)
        if path == f"/nodes/{self.template.node}/status":
            return json.dumps({"cpuinfo": {"cpus": 24}, "cpu": 0, "loadavg": ["1"]})
        storage = self.request["host"].get("cloudinit_snippet_storage")
        if storage and path == f"/nodes/{self.template.node}/storage/{storage}/status":
            return json.dumps(self.snippet_status)
        for vmid, guest in self.guests.items():
            prefix = f"/nodes/{self.template.node}/qemu/{vmid}/"
            if path.startswith(prefix):
                kind = path.removeprefix(prefix)
                if kind == "snapshot":
                    return json.dumps(
                        [
                            {
                                "name": name,
                                **{
                                    k: v
                                    for k, v in snap.items()
                                    if k not in {"config", "disk_state"}
                                },
                            }
                            for name, snap in self.snapshots.get(vmid, {}).items()
                        ]
                        + [{"name": "current"}]
                    )
                if kind.startswith("snapshot/") and kind.endswith("/config"):
                    snap = self.snapshots[vmid][kind.split("/")[1]]
                    return json.dumps(
                        snap["config"]
                        | {"description": snap["description"], "snaptime": snap["snaptime"]}
                    )
                return json.dumps(
                    {
                        "config": guest["config"],
                        "pending": guest["pending"],
                        "status/current": {"status": guest["status"]},
                        "feature": {"hasFeature": 1},
                    }[kind]
                )
        return self.template(argv)


class TestNativeLifecycle(unittest.TestCase):
    def setUp(self):
        self.req = request()
        self.fixture = GuestNativeFixture(self.req)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        t = guest_host.template_host
        self.stack.enter_context(patch.object(t, "command", side_effect=self.fixture))
        self.stack.enter_context(patch.object(guest_host, "command", side_effect=self.fixture))
        self.stack.enter_context(patch.object(t.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(t.platform, "system", return_value="Linux"))
        self.stack.enter_context(patch.object(t.platform, "machine", return_value="x86_64"))
        self.stack.enter_context(patch.object(t.shutil, "which", return_value="/tool"))
        self.stack.enter_context(patch.object(guest_verify, "kvm_probe", return_value=12))
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(t, "LOCKS", Path(self.temp) / "locks"))
        self.fixture.snippets = Path(self.temp) / "snippets"
        self.fixture.snippets.mkdir(mode=0o755)
        self.fixture.nodes = Path(self.temp) / "nodes"
        self.stack.enter_context(patch.object(guest_host, "PVE_NODES", self.fixture.nodes))
        original_read = Path.read_text
        self.stack.enter_context(
            patch.object(
                Path,
                "read_text",
                lambda path, *a, **kw: (
                    {
                        "/proc/cpuinfo": "flags : vmx",
                        "/proc/meminfo": "MemAvailable: 33554432 kB\n",
                        "/sys/module/kvm_intel/parameters/nested": "Y",
                    }.get(str(path))
                    if str(path)
                    in {"/proc/cpuinfo", "/proc/meminfo", "/sys/module/kvm_intel/parameters/nested"}
                    else original_read(path, *a, **kw)
                ),
            )
        )
        original_file = Path.is_file
        self.stack.enter_context(
            patch.object(
                Path,
                "is_file",
                lambda path: (
                    True
                    if str(path) == "/sys/module/kvm_intel/parameters/nested"
                    else original_file(path)
                ),
            )
        )
        t.create(self.req["template"], "unused", "/unused")
        self.fixture.template(["qm", "template", "9001"])
        self.fixture.template.config["description"] = (
            "kdive-template-v1:" + t.identity(self.req["template"]) + ":ready"
        )
        self.fixture.calls.clear()

    def execute(self, mode, ack=None):
        output = io.StringIO()

        def acknowledge(timeout):
            if ack is not None:
                return ack
            event = json.loads(output.getvalue().splitlines()[-1])
            return {
                "vmid": 1101,
                "identity": guest_host.identity(self.req),
                "verified": True,
                "phase": "post-reboot" if event["phase"] == "reboot" else event["phase"],
            }

        with (
            patch.object(guest_host, "read_line", side_effect=acknowledge),
            contextlib.redirect_stdout(output),
        ):
            guest_host.session([self.req], mode)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_replayed_preparation_ack_cannot_promote_after_reboot_request(self):
        guest_host.clone(self.req)
        self.req["host"]["profile"] = "opensuse"
        config = self.fixture.guests[1101]["config"]
        config["description"] = guest_host.marker(self.req, "preparing")
        ack = {
            "vmid": 1101,
            "identity": guest_host.identity(self.req),
            "verified": True,
            "phase": "prepared",
        }
        with (
            patch.object(guest_host, "read_line", side_effect=[ack, ack]),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(guest_host.GuestError),
        ):
            guest_host.verify_readiness(self.req, fresh=True)
        self.assertTrue(config["description"].endswith(":preparing"))

    def test_opensuse_ready_requires_two_acks_and_no_native_power_action(self):
        self.assertTrue(callable(getattr(guest_host, "verify_readiness", None)))
        guest_host.clone(self.req)
        self.req["host"]["profile"] = "opensuse"
        config = self.fixture.guests[1101]["config"]
        config["description"] = guest_host.marker(self.req, "preparing")
        ack = {
            "vmid": 1101,
            "identity": guest_host.identity(self.req),
            "verified": True,
            "phase": "prepared",
        }
        for fail_at in (0, 1):
            output = io.StringIO()
            effects = [ack] * fail_at + [guest_host.GuestError("deadline")]
            with (
                patch.object(guest_host, "read_line", side_effect=effects),
                contextlib.redirect_stdout(output),
                self.assertRaises(guest_host.GuestError),
            ):
                guest_host.verify_readiness(self.req, fresh=True)
            self.assertTrue(config["description"].endswith(":preparing"))
            self.assertEqual(
                sum(
                    json.loads(line)["phase"] == "reboot" for line in output.getvalue().splitlines()
                ),
                fail_at,
            )
        self.fixture.calls.clear()
        output = io.StringIO()
        with (
            patch.object(
                guest_host, "read_line", side_effect=[ack, dict(ack, phase="post-reboot")]
            ) as read,
            contextlib.redirect_stdout(output),
        ):
            guest_host.verify_readiness(self.req, fresh=True)
        self.assertEqual([c.args[0] for c in read.call_args_list], [2500, 2700])
        self.assertTrue(config["description"].endswith(":ready"))
        self.assertEqual([c[1] for c in self.fixture.calls if c[0] == "qm"], ["agent", "set"])
        self.assertIn("--digest", next(c for c in self.fixture.calls if c[:2] == ["qm", "set"]))

    def test_clone_configures_guest_vlan_without_changing_source(self):
        initial = copy.deepcopy(self.fixture.template.config)
        for source_vlan, guest_vlan in itertools.product((None, 30), (None, 25)):
            with self.subTest(source=source_vlan, guest=guest_vlan):
                self.fixture.guests.clear()
                self.fixture.snapshots.clear()
                self.fixture.volumes.clear()
                self.fixture.template.config = copy.deepcopy(initial)
                self.req["host"].pop("vlan", None)
                if guest_vlan is not None:
                    self.req["host"]["vlan"] = guest_vlan
                self.req["template"]["vlan"] = source_vlan
                if source_vlan is not None:
                    self.fixture.template.config["net0"] += f",tag={source_vlan}"
                self.fixture.template.config["description"] = (
                    "kdive-template-v1:"
                    + guest_host.template_host.identity(self.req["template"])
                    + ":ready"
                )
                original = copy.deepcopy(self.fixture.template.config)
                self.execute("apply")
                net = guest_host.template_host.properties(
                    self.fixture.guests[1101]["config"]["net0"]
                )
                self.assertEqual(net.get("tag"), str(guest_vlan) if guest_vlan else None)
                self.assertEqual(net["virtio"], "02:00:00:00:04:4d")
                self.assertNotEqual(
                    net["virtio"], guest_host.template_host.properties(original["net0"])["virtio"]
                )
                self.assertEqual(self.fixture.template.config, original)
                self.fixture.calls.clear()
                self.execute("apply")
                self.assertFalse(
                    any(c[:2] in (["qm", "set"], ["qm", "clone"]) for c in self.fixture.calls)
                )

    def test_clone_applies_all_guest_settings_and_rejects_drift(self):
        original = copy.deepcopy(self.fixture.template.config)
        self.req["host"].update(
            cpu="x86-64-v3",
            storage="guestpool",
            bridge="vmbr2",
            vlan=12,
            nic_queues=4,
            balloon_mib=2048,
        )
        self.fixture.destination_extent = 16 * 1024**2
        self.fixture.template.extent = self.fixture.destination_extent
        read = self.fixture.read

        def destination(argv):
            if argv[2] == "/storage/guestpool":
                return '{"type":"lvmthin","vgname":"vg-example"}'
            if argv[2].endswith("/storage/guestpool/status"):
                return json.dumps(dict(self.fixture.template.storage, type="lvmthin"))
            if argv[2].endswith("/network"):
                return json.dumps([dict(self.fixture.template.bridge, iface="vmbr2")])
            return read(argv)

        with patch.object(self.fixture, "read", side_effect=destination):
            self.execute("apply")
            config = self.fixture.guests[1101]["config"]
            self.assertEqual(config["cpu"], "x86-64-v3")
            self.assertEqual(config["balloon"], "2048")
            self.assertTrue(config["scsi0"].startswith("guestpool:"))
            self.assertIn("bridge=vmbr2,tag=12,queues=4", config["net0"])
            self.assertEqual(self.fixture.template.config, original)
            self.execute("verify")
            for key, value in (
                ("cpu", "host"),
                ("balloon", "0"),
                ("net0", config["net0"].replace("queues=4", "queues=2")),
            ):
                saved = config[key]
                config[key] = value
                with self.subTest(key=key), self.assertRaises(guest_host.GuestError):
                    self.execute("verify")
                config[key] = saved

    def test_large_source_extents_survive_smaller_destination_geometry(self):
        self.req["host"]["storage"] = "guestpool"
        self.fixture.template.storage["type"] = "lvmthin"
        self.fixture.template.extent = 16 * 1024**2
        for volume in self.fixture.template.volumes:
            volume["size"] = guest_host.template_host.allocated_size(
                volume["size"], self.fixture.template.extent
            )
        original_read = self.fixture.read
        original_command = self.fixture.__call__
        for destination_extent in (0, 4 * 1024**2):
            self.fixture.guests.clear()
            self.fixture.volumes.clear()
            self.fixture.destination_extent = destination_extent
            capacity = {"avail": 100 * 1024**3}

            def read(argv, destination_extent=destination_extent, capacity=capacity):
                if argv[2] == "/storage/guestpool":
                    return '{"type":"lvmthin","vgname":"vg-destination"}'
                if argv[2].endswith("/storage/guestpool/status"):
                    return json.dumps(
                        dict(
                            self.fixture.template.storage,
                            type="lvmthin" if destination_extent else "zfspool",
                            avail=capacity["avail"],
                        )
                    )
                return original_read(argv)

            def command(argv, destination_extent=destination_extent, **kwargs):
                if argv[0] == "vgs" and argv[-1] == "vg-destination":
                    return json.dumps(
                        {
                            "report": [
                                {
                                    "vg": [
                                        {
                                            "vg_name": "vg-destination",
                                            "vg_extent_size": str(destination_extent),
                                        }
                                    ]
                                }
                            ]
                        }
                    )
                return original_command(argv, **kwargs)

            with (
                self.subTest(extent=destination_extent),
                patch.object(self.fixture, "read", side_effect=read),
                patch.object(guest_host.template_host, "command", side_effect=command),
            ):
                self.execute("apply")
                self.execute("verify")
                self.fixture.guests.clear()
                self.fixture.snapshots.clear()
                self.fixture.volumes.clear()
                self.fixture.calls.clear()
                capacity["avail"] = 32 * 1024**3 + 2 * 16 * 1024**2 - 1
                with self.assertRaisesRegex(guest_host.GuestError, "storage space"):
                    self.execute("apply")
                self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def oversubscribed_node(self):
        original_read = self.fixture.read

        def read(argv):
            if argv[2] == f"/nodes/{self.fixture.template.node}/status":
                return json.dumps({"cpuinfo": {"cpus": 2}, "cpu": 0, "loadavg": ["1"]})
            return original_read(argv)

        return patch.object(self.fixture, "read", side_effect=read)

    def test_oversubscribed_batch_warns_and_proceeds(self):
        warning = {"phase": "capacity-warning", "resource": "cpu", "requested": 2, "available": 1}
        with self.oversubscribed_node():
            events = self.execute("plan")
            self.assertEqual(events[0], warning)
            self.assertEqual(events[1]["action"], "would-create")
            events = self.execute("apply")
        self.assertEqual(events[0], warning)
        self.assertEqual(events[-1]["action"], "created")

    def test_verify_of_absent_guest_fails_without_capacity_warning(self):
        output = io.StringIO()
        with (
            self.oversubscribed_node(),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(guest_host.GuestError, "Verify requires existing") as caught,
        ):
            guest_host.session([self.req], "verify")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(caught.exception.code, "guests-missing")

    def test_legacy_disabled_guest_without_explicit_shares_verifies(self):
        self.req["host"]["balloon_mib"] = 0
        self.execute("apply")
        self.fixture.guests[1101]["config"].pop("shares")
        self.assertEqual(self.execute("verify")[-1]["action"], "preserved")

    def test_source_api_native_disagreement_fails_before_write(self):
        self.fixture.template.config["net0"] += ",tag=30"
        self.fixture.template.config["description"] = (
            "kdive-template-v1:"
            + guest_host.template_host.identity(dict(self.req["template"], vlan=30))
            + ":ready"
        )
        with self.assertRaisesRegex(guest_host.GuestError, "Template identity/phase"):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_tagged_guest_requires_network_support_even_with_untagged_source(self):
        self.req["host"]["vlan"] = 25
        self.fixture.template.bridge.update(bridge_vlan_aware=0, bridge_ports="")
        with self.assertRaisesRegex(guest_host.GuestError, "Tagged network"):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_changed_source_vlan_cannot_be_adopted_without_matching_identity(self):
        self.fixture.template.config["net0"] += ",tag=30"
        self.req["template"]["vlan"] = 30
        self.req["host"]["vlan"] = 25
        with self.assertRaisesRegex(guest_host.GuestError, "Template identity/phase"):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_fresh_ready_rerun_and_selected_drift_preserve_resources(self):
        events = self.execute("plan")
        self.assertEqual(events[0]["action"], "would-create")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))
        self.assertFalse((Path(self.temp) / "locks").exists())
        events = self.execute("apply")
        self.assertEqual(events[-1]["action"], "created")
        self.assertTrue(self.fixture.guests[1101]["config"]["description"].endswith(":ready"))
        writes = [c[1] for c in self.fixture.calls if c[0] == "qm"]
        self.assertEqual(
            writes,
            [
                "clone",
                "set",
                "resize",
                "set",
                "set",
                "start",
                "agent",
                "set",
                "shutdown",
                "snapshot",
                "start",
                "agent",
            ],
        )
        self.fixture.calls.clear()
        self.assertEqual(self.execute("apply")[-1]["action"], "preserved")
        self.assertEqual([c[1] for c in self.fixture.calls if c[0] == "qm"], ["agent"])
        self.fixture.guests[1101]["config"]["onboot"] = "1"
        self.fixture.calls.clear()
        with self.assertRaises(guest_host.GuestError):
            self.execute("apply")
        self.assertEqual(self.fixture.guests[1101]["config"]["onboot"], "1")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_failed_ack_leaves_preparing_and_rerun_refuses(self):
        with self.assertRaises(guest_host.GuestError):
            self.execute(
                "apply",
                {
                    "vmid": 1102,
                    "identity": guest_host.identity(self.req),
                    "verified": True,
                    "phase": "prepared",
                },
            )
        self.assertTrue(self.fixture.guests[1101]["config"]["description"].endswith(":preparing"))
        self.fixture.calls.clear()
        with self.assertRaises(guest_host.GuestError):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_fresh_seed_uses_scsi_and_preserves_template_root_efi_and_uuid(self):
        template = copy.deepcopy(self.fixture.template.config)
        self.execute("apply")
        config = self.fixture.guests[1101]["config"]
        self.assertNotIn("ide2", config)
        self.assertIn("cloudinit,media=cdrom", config["scsi1"])
        self.assertEqual(self.fixture.template.config, template)
        self.assertEqual(config["smbios1"], "uuid=00000000-0000-4000-8000-000000001101")
        self.assertIn("vm-1101-disk-0", config["scsi0"])
        self.assertIn("vm-1101-disk-1", config["efidisk0"])
        removals = [c for c in self.fixture.calls if "--delete" in c]
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0][removals[0].index("--delete") + 1], "ide2")
        self.assertIn("--digest", removals[0])
        self.assertEqual(len(self.fixture.volumes), 3)

    def test_unowned_or_invalid_seed_is_never_removed(self):
        for invalid in ("foreign", "root_disk", "oversized", "pending"):
            with self.subTest(invalid=invalid):
                self.fixture.guests.clear()
                self.fixture.snapshots.clear()
                self.fixture.volumes.clear()
                self.fixture.calls.clear()

                def command(argv, invalid=invalid, **kwargs):
                    result = self.fixture(argv, **kwargs)
                    if argv[:2] == ["qm", "resize"]:
                        guest = self.fixture.guests[1101]
                        if invalid == "foreign":
                            guest["config"]["ide2"] = "pool:vm-9999-cloudinit,media=cdrom"
                        elif invalid == "root_disk":
                            guest["config"]["ide2"] = guest["config"]["scsi0"]
                        elif invalid == "pending":
                            guest["pending"] = [{"key": "ide2", "delete": 1}]
                        else:
                            for volume in self.fixture.volumes:
                                if volume["volid"].endswith("cloudinit"):
                                    volume["size"] = 1024**3
                    return result

                with patch.object(guest_host, "command", side_effect=command):
                    with self.assertRaises(guest_host.GuestError):
                        self.execute("apply")
                self.assertFalse(any("--delete" in c for c in self.fixture.calls))
                self.assertEqual(self.fixture.guests[1101]["status"], "stopped")

    def test_nonseed_change_during_replacement_stops_before_attach(self):
        def command(argv, **kwargs):
            result = self.fixture(argv, **kwargs)
            if "--delete" in argv:
                self.fixture.guests[1101]["config"]["smbios1"] = (
                    "uuid=00000000-0000-4000-8000-000000009999"
                )
            return result

        with patch.object(guest_host, "command", side_effect=command):
            with self.assertRaises(guest_host.GuestError):
                self.execute("apply")
        self.assertFalse(any("--scsi1" in c for c in self.fixture.calls))
        self.assertEqual(self.fixture.guests[1101]["status"], "stopped")

    def test_seed_allocation_failure_retains_root_and_efi_without_cleanup(self):
        def command(argv, **kwargs):
            if "--scsi1" in argv:
                raise guest_host.GuestError("Native seed allocation failed")
            return self.fixture(argv, **kwargs)

        with patch.object(guest_host, "command", side_effect=command):
            with self.assertRaises(guest_host.GuestError):
                self.execute("apply")
        guest = self.fixture.guests[1101]
        self.assertEqual(guest["status"], "stopped")
        self.assertTrue(guest["config"]["description"].endswith(":preparing"))
        self.assertEqual(len(self.fixture.volumes), 2)
        self.assertTrue(all(not v["volid"].endswith("cloudinit") for v in self.fixture.volumes))
        self.assertIn("vm-1101-disk-0", guest["config"]["scsi0"])
        self.assertIn("vm-1101-disk-1", guest["config"]["efidisk0"])
        self.assertNotIn("destroy", [c[1] for c in self.fixture.calls if c[0] == "qm"])

    def test_slow_successful_guest_can_acknowledge_within_controller_budget(self):
        reader, writer = os.pipe()
        ack = {
            "vmid": 1101,
            "identity": guest_host.identity(self.req),
            "verified": True,
            "phase": "prepared",
        }
        os.write(
            writer,
            (json.dumps(ack) + "\n" + json.dumps(dict(ack, phase="baseline-boot")) + "\n").encode(),
        )
        os.close(writer)
        read_line = guest_host.read_line
        with os.fdopen(reader, "rb", buffering=0) as stream:

            def delayed_ack(timeout):
                with (
                    patch.object(guest_host.sys, "stdin", stream),
                    patch.object(
                        guest_host.time,
                        "monotonic",
                        side_effect=itertools.chain([0], itertools.repeat(1900)),
                    ),
                ):
                    return read_line(timeout)

            with (
                patch.object(guest_host, "read_line", side_effect=delayed_ack),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                guest_host.session([self.req], "apply")
        self.assertTrue(self.fixture.guests[1101]["config"]["description"].endswith(":ready"))

    def test_clone_failure_does_not_continue_or_delete(self):
        self.fixture.fail_clone = True
        with self.assertRaises(guest_host.GuestError):
            self.execute("apply")
        self.assertEqual([c[1] for c in self.fixture.calls if c[0] == "qm"], ["clone"])

    def test_foreign_identity_is_not_repaired(self):
        self.fixture.guests[1101] = {
            "config": {"description": "foreign"},
            "status": "stopped",
            "pending": [],
        }
        with self.assertRaises(guest_host.GuestError):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_invalid_request_acquires_no_lock(self):
        self.req["host"]["vmid"] = "../../unsafe"
        with self.assertRaises(ValidationError):
            self.execute("apply")
        self.assertFalse((Path(self.temp) / "locks").exists())

    def test_unselected_resource_is_unchanged(self):
        other = {
            "config": {"description": "unrelated", "onboot": "1"},
            "status": "running",
            "pending": [],
        }
        self.fixture.guests[1102] = copy.deepcopy(other)
        self.execute("apply")
        self.assertEqual(self.fixture.guests[1102], other)

    def test_failed_native_read_is_not_absence(self):
        with patch.object(
            guest_host.template_host,
            "command",
            side_effect=guest_host.GuestError("Native command failed"),
        ):
            with self.assertRaises(guest_host.GuestError):
                self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))


class TestController(unittest.TestCase):
    def test_ssh_pin_path_is_one_literal_native_filename(self):
        path = Path('/tmp/private%p inventory "quoted"/known_hosts')
        for fresh in (True, False):
            argv = guests.guest_ssh(request()["host"], path, fresh)
            option = next(value for value in argv if value.startswith("UserKnownHostsFile="))
            result = subprocess.run(
                [
                    "ssh",
                    "-vvv",
                    "-F",
                    "/dev/null",
                    "-o",
                    "IdentityFile=none",
                    "-o",
                    "ProxyCommand=false",
                    "-o",
                    option,
                    "192.0.2.11",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 255)
            expanded = [
                line for line in result.stderr.splitlines() if "expanded UserKnownHostsFile" in line
            ]
            self.assertEqual(len(expanded), 1)
            self.assertTrue(expanded[0].endswith(" -> '" + str(path) + "'"), expanded)
        path = path.parent / "back\\slash" / "known_hosts"
        result = subprocess.run(
            ["ssh", "-G", "-F", "/dev/null", "-o", guests.known_hosts_option(path), "192.0.2.11"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertIn("userknownhostsfile " + str(path), result.stdout.splitlines())

    def test_ssh_pin_path_rejects_expansion_before_native_dispatch(self):
        for value in ("/private/${HOME}/known_hosts", "/private/line\nfeed/known_hosts"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                guests.guest_ssh(request()["host"], Path(value), fresh=True)
        data = load_inventory(ROOT / "inventory/example.yml")
        with (
            patch.object(guests, "load_inventory", return_value=data),
            patch.object(
                guests.sys,
                "argv",
                [
                    "guests.py",
                    "--inventory",
                    "/private/${HOME}/inventory.yml",
                    "--targets",
                    "ubuntu",
                    "--apply",
                ],
            ),
            patch.object(guests.templates, "api_admission") as admission,
            patch.object(guests, "dispatch") as dispatch,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(guests.main(), 1)
            admission.assert_not_called()
            dispatch.assert_not_called()

    def test_partial_event_cannot_block_past_read_deadline(self):
        reader, writer = os.pipe()
        stream = os.fdopen(reader, "rb", buffering=0)
        real_select = select.select

        def alarm(signum, frame):
            raise TimeoutError("A partial event blocked after readiness")

        previous = signal.signal(signal.SIGALRM, alarm)
        try:
            os.write(writer, b"{")
            signal.alarm(2)
            with patch.object(
                guests.select, "select", side_effect=lambda r, w, x, _: real_select(r, w, x, 0.01)
            ):
                with self.assertRaises(ValidationError):
                    guests.read_event(SimpleNamespace(stdout=stream))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
            stream.close()
            os.close(writer)

    def test_batch_plan_consumes_back_to_back_events(self):
        requests = [request(), request()]
        requests[1]["host"]["vmid"] += 1
        events = [
            {
                "vmid": r["host"]["vmid"],
                "identity": guest_host.identity(r),
                "phase": "planned",
                "action": "would-create",
            }
            for r in requests
        ]
        source = (
            "import sys; sys.stdin.readline(); print("
            + repr("\n".join(json.dumps(event) for event in events))
            + ", flush=True); sys.stdin.read()"
        )
        real_select = select.select
        with (
            patch.object(
                guests, "host_ssh", return_value=[str(ROOT / ".venv/bin/python"), "-c", source]
            ),
            patch.object(
                guests.select, "select", side_effect=lambda r, w, x, _: real_select(r, w, x, 1)
            ),
        ):
            self.assertEqual(len(guests.dispatch(requests, "plan", Path("/unused"))), 2)

    def capacity_stub(self, warnings, error=False):
        return [
            str(ROOT / ".venv/bin/python"),
            "-c",
            "import json, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from scripts import guest_host\n"
            "envelope = json.loads(sys.stdin.readline())\n"
            f"for warning in {warnings!r}:\n"
            "    print(json.dumps(warning), flush=True)\n"
            f"if {error!r}:\n"
            "    print(json.dumps({'error': 'x'}), flush=True)\n"
            "for r in envelope['requests']:\n"
            "    print(json.dumps({'vmid': r['host']['vmid'], 'identity': guest_host.identity(r),"
            " 'phase': 'planned', 'action': 'would-create'}), flush=True)\n"
            "sys.stdin.read()\n",
        ]

    def test_capacity_warnings_reach_operator_and_reject_malformed_events(self):
        cpu = {"phase": "capacity-warning", "resource": "cpu", "requested": 8, "available": 2}
        memory = dict(cpu, resource="memory", requested=4096, available=1024)
        req = request()
        stderr = io.StringIO()
        with (
            patch.object(guests, "host_ssh", return_value=self.capacity_stub([cpu, memory])),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(len(guests.dispatch([req], "plan", Path("/unused"))), 1)
        node = req["host"]["proxmox_node"]
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                f"Guest capacity warning: node {node} new guests request 8 vCPUs; "
                "2 logical CPUs observed free; guests may contend for CPU",
                f"Guest capacity warning: node {node} new guests request 4096 MiB RAM; "
                "1024 MiB MemAvailable observed; guests may contend for memory",
            ],
        )
        for warnings in [
            [cpu, cpu],
            [dict(cpu, extra=1)],
            [dict(cpu, requested=True, available=0)],
            [dict(cpu, available=2.0)],
            [dict(cpu, available=8)],
            [dict(cpu, available=-1)],
            [dict(cpu, resource="disk")],
        ]:
            with (
                self.subTest(warnings=warnings),
                patch.object(guests, "host_ssh", return_value=self.capacity_stub(warnings)),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaisesRegex(ValidationError, "capacity warning"),
            ):
                guests.dispatch([req], "plan", Path("/unused"))

    def test_capacity_warnings_from_each_host_group(self):
        data = load_inventory(ROOT / "inventory/example.yml")
        for alias in ("fedora", "fedora-install"):
            data["_meta"]["hostvars"][alias]["proxmox_node"] = "other-node"
        cpu = {"phase": "capacity-warning", "resource": "cpu", "requested": 8, "available": 2}

        resolved = {}
        for vmid, node in ((1101, "example-node"), (1102, "other-node")):
            resolved[vmid] = request()
            resolved[vmid]["host"].update(vmid=vmid, proxmox_node=node)
            resolved[vmid]["template"]["node"] = node

        for failing in (False, True):
            stderr = io.StringIO()
            with (
                self.subTest(failing=failing),
                patch.object(guests, "load_inventory", return_value=data),
                patch.object(guests.templates, "api_admission"),
                patch.object(
                    guests, "request_for", side_effect=lambda host, *_: resolved[host["vmid"]]
                ),
                patch.object(
                    guests,
                    "host_ssh",
                    side_effect=lambda host, failing=failing: self.capacity_stub(
                        [cpu], failing and host["proxmox_node"] == "other-node"
                    ),
                ),
                patch.object(guests.sys, "argv", ["guests.py", "--targets", "ubuntu,fedora"]),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(guests.main(), 1 if failing else 0)
            warnings = [
                line for line in stderr.getvalue().splitlines() if "capacity warning" in line
            ]
            self.assertEqual([line.split()[4] for line in warnings], ["example-node", "other-node"])

    def test_host_key_pins_are_private_and_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "known_hosts"
            guests.prepare_known_hosts(path, fresh=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("private-key-pin\n")
            guests.prepare_known_hosts(path, fresh=True)
            self.assertEqual(path.read_text(), "private-key-pin\n")
            path.chmod(0o644)
            with self.assertRaises(ValidationError):
                guests.prepare_known_hosts(path, fresh=False)

    def test_ssh_trust_is_fresh_only_and_private_key_is_local(self):
        host = request()["host"]
        for fresh, policy in [(True, "accept-new"), (False, "yes")]:
            argv = guests.guest_ssh(host, Path("/private/known_hosts"), fresh)
            self.assertIn("StrictHostKeyChecking=" + policy, argv)
            self.assertIn("BatchMode=yes", argv)
            self.assertNotIn("StrictHostKeyChecking=no", argv)

    def test_cli_rejects_missing_and_pattern_selection_offline(self):
        for arguments in [[], ["--targets", "*"]]:
            result = subprocess.run(
                [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/guests.py"), *arguments],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)

    def test_results_bind_identity_and_exclude_private_fields(self):
        req = request()
        result = {
            "vmid": req["host"]["vmid"],
            "identity": guest_host.identity(req),
            "phase": "prepared",
            "fresh": True,
            "guest_uuid": "12345678-1234-1234-1234-123456789abc",
        }
        guests.validate_event(req, result, "prepared")
        with self.assertRaises(ValidationError):
            guests.validate_event(req, dict(result, identity="b" * 64), "prepared")
        with self.assertRaises(ValidationError):
            guests.validate_event(req, dict(result, fqdn="secret.example.invalid"), "prepared")
        for value in (None, "bad", "00000000-0000-0000-0000-000000000000"):
            with self.assertRaises(ValueError):
                guests.validate_event(req, dict(result, guest_uuid=value), "prepared")


class TestRebootExchange(unittest.TestCase):
    def exercise_exchange(self, fault=None):
        import sys

        req = request()
        req["host"]["profile"] = "opensuse"
        req["template"]["image"]["release"] = "16.0"
        guest_id = "12345678-1234-1234-1234-123456789abc"
        old = "12345678-1234-1234-1234-123456789abd"
        new = "12345678-1234-1234-1234-123456789abe"
        prepared = dict(
            vmid=1101,
            identity=guest_host.identity(req),
            phase="prepared",
            fresh=True,
            guest_uuid=guest_id,
        )
        reboot = {k: v for k, v in prepared.items() if k != "fresh"} | {"phase": "reboot"}
        ready = dict(
            vmid=1101,
            identity=guest_host.identity(req),
            phase="ready",
            action="created",
            config_sha256="a" * 64,
            duration_seconds=1,
            snapshot="clean",
            snapshot_identity="b" * 64,
            snapshot_config_sha256="c" * 64,
            snapshot_time=123456,
        )
        baseline_boot = {k: v for k, v in prepared.items() if k != "fresh"} | {
            "phase": "baseline-boot"
        }
        if fault == "uuid":
            reboot["guest_uuid"] = new
        if fault == "missing":
            reboot = ready
        if fault == "repeat":
            ready = reboot
        code = (
            "import sys,json;json.loads(sys.stdin.readline());print("
            + repr(json.dumps(prepared))
            + ",flush=True);assert json.loads(sys.stdin.readline())['phase'] == 'prepared';print("
            + repr(json.dumps(reboot))
            + ",flush=True);assert json.loads(sys.stdin.readline())['phase'] == "
            "'post-reboot';print("
            + repr(json.dumps(baseline_boot))
            + ",flush=True);assert json.loads(sys.stdin.readline())['phase'] == "
            "'baseline-boot';print(" + repr(json.dumps(ready)) + ",flush=True)"
        )
        boot, effects = old, []

        def ssh(argv, **kwargs):
            nonlocal boot
            output = ""
            if argv[-1] == "cat /proc/sys/kernel/random/boot_id":
                if effects == ["prepare", "reboot", "verify"]:
                    boot = "12345678-1234-1234-1234-123456789abf"
                output = boot
            elif argv[-1] != "true":
                envelope = json.loads(kwargs["input"])
                if "reboot_from" in envelope:
                    effects.append("reboot")
                    self.assertEqual(envelope["reboot_from"], old)
                    boot = old if fault == "disconnect-unchanged" else new
                    if fault in {"disconnect", "disconnect-unchanged", "disconnect-post-boot"}:
                        return subprocess.CompletedProcess(argv, 255, "", "disconnected")
                    if fault == "reboot-error":
                        return subprocess.CompletedProcess(argv, 1, "", "guest error")
                    if fault == "partial":
                        return subprocess.CompletedProcess(argv, 255, "{", "disconnected")
                    if fault == "ack-disconnect":
                        return subprocess.CompletedProcess(
                            argv,
                            255,
                            json.dumps({"reboot_requested": True}),
                            "Connection to guest closed by remote host.\n",
                        )
                    output = (
                        "null" if fault == "invalid-ack" else json.dumps({"reboot_requested": True})
                    )
                else:
                    effects.append("prepare" if envelope["fresh"] else "verify")
                    output = json.dumps(
                        dict(
                            guest_uuid=guest_id,
                            boot_id=(
                                old
                                if fault in {"post-boot", "disconnect-post-boot"}
                                and not envelope["fresh"]
                                else boot
                            ),
                            os_id="opensuse-leap",
                            release="16.0",
                            architecture="x86_64",
                            hostname=req["host"]["fqdn"].split(".")[0],
                            fqdn=req["host"]["fqdn"],
                            ipv4=[req["host"]["ansible_host"]],
                            cpus=2,
                            memory_bytes=4 * 1024**3,
                            crash_reserved_bytes=0,
                            balloon_driver=True,
                            filesystem_bytes=31 * 1024**3,
                            security="selinux-enforcing",
                            kvm_api=12,
                            kvm_create_vm=True,
                            packages={
                                n: "1"
                                for n in (
                                    "cloud-init",
                                    "openssh-server",
                                    "sudo",
                                    "qemu-guest-agent",
                                    "python3-base",
                                )
                            }
                            | {n: "0:" + v for n, v in guest_verify.OPENSUSE_PACKAGES.items()},
                        )
                    )
            return subprocess.CompletedProcess(argv, 0, output, "")

        wait = guests.wait_guest

        def bounded_wait(*args):
            if fault == "disconnect-unchanged" and args[-1]:
                with patch.object(guests.time, "monotonic", side_effect=[0, 601]):
                    return wait(*args)
            return wait(*args)

        with tempfile.TemporaryDirectory() as directory:
            pins = Path(directory) / "known_hosts"
            with (
                patch.object(guests, "host_ssh", return_value=[sys.executable, "-u", "-c", code]),
                patch.object(guests, "run_guest", side_effect=ssh),
                patch.object(guests, "wait_guest", side_effect=bounded_wait),
            ):
                if fault and fault not in {"disconnect", "ack-disconnect"}:
                    with self.assertRaises(ValueError):
                        guests.dispatch([req], "apply", pins)
                    self.assertEqual(
                        effects.count("reboot"), 0 if fault in {"uuid", "missing"} else 1
                    )
                    return
                outcomes = guests.dispatch([req], "apply", pins)
        self.assertEqual(effects, ["prepare", "reboot", "verify", "verify"])
        self.assertNotIn("boot_id", outcomes[0])
        self.assertNotIn("guest_uuid", outcomes[0])

    def test_controller_requires_changed_boot_and_hides_boot_identity(self):
        self.exercise_exchange()

    def test_failed_or_repeated_reboot_exchange_never_retries(self):
        for fault in (
            "uuid",
            "missing",
            "repeat",
            "reboot-error",
            "post-boot",
            "disconnect-unchanged",
            "disconnect-post-boot",
            "partial",
            "invalid-ack",
        ):
            with self.subTest(fault=fault):
                self.exercise_exchange(fault)

    def test_disconnect_after_single_reboot_requires_full_postboot_proof(self):
        self.exercise_exchange("disconnect")

    def test_acknowledged_reboot_disconnect_requires_full_postboot_proof(self):
        self.exercise_exchange("ack-disconnect")

    def test_disconnect_during_other_rpc_remains_failure(self):
        req = request()
        for output in ("", json.dumps({"reboot_requested": True})):
            with patch.object(
                guests,
                "run_guest",
                return_value=subprocess.CompletedProcess([], 255, output, ""),
            ):
                for envelope in ({"fresh": True}, {"fresh": False}):
                    with (
                        self.subTest(envelope=envelope, output=output),
                        self.assertRaises(ValidationError),
                    ):
                        guests.guest_rpc(req, Path("/unused"), envelope, 1)


class TestBoundedGuestOutput(unittest.TestCase):
    def run_child(self, source, operation, *, timeout=2):
        import sys
        import time

        children = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        argv = [sys.executable, "-u", "-c", source]
        started = time.monotonic()
        try:
            with (
                patch.object(guests, "guest_ssh", return_value=argv),
                patch.object(guests.subprocess, "Popen", side_effect=start),
            ):
                return operation(argv, timeout)
        finally:
            for child in children:
                try:
                    self.assertIsNotNone(child.returncode, "transport was not reaped")
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    for stream in (child.stdin, child.stdout, child.stderr):
                        if stream is not None:
                            stream.close()
            self.assertLess(time.monotonic() - started, timeout + 6)

    def rpc(self, source, envelope=None):
        return self.run_child(
            source,
            lambda argv, timeout: guests.guest_rpc(
                request(), Path("/unused"), envelope or {"fresh": False}, timeout
            ),
        )

    def test_each_stream_and_combined_overflow_reap_before_timeout(self):
        for source in (
            "os.write(1, b'x' * 65537)",
            "os.write(2, b'x' * 65537)",
            "os.write(1, b'x' * 32768); os.write(2, b'x' * 32769)",
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValidationError, "^Guest SSH: response exceeds limit;"),
            ):
                self.rpc("import os,time; " + source + "; time.sleep(30)")

    def test_exact_boundary_valid_json_and_multibyte_output(self):
        for value in ("x" * 65534, "é" * 32767):
            with self.subTest(multibyte=value[0] != "x"):
                source = (
                    "import os,json; os.write(1,json.dumps("
                    + repr(value[0])
                    + "*"
                    + str(len(value))
                    + ",ensure_ascii=False).encode())"
                )
                self.assertEqual(self.rpc(source), value)

    def test_partial_json_and_invalid_encoding_are_sanitized(self):
        for output, message in (
            (b'{"private":', "Guest baseline: invalid result"),
            (b"\xffprivate", "Guest SSH: invalid response encoding"),
        ):
            with self.subTest(output=output), self.assertRaisesRegex(ValidationError, message):
                self.rpc("import os; os.write(1, " + repr(output) + ")")

    def test_deeply_nested_json_is_sanitized(self):
        source = "import os; os.write(1, b'[' * 32767 + b']' * 32767)"
        with self.assertRaisesRegex(ValidationError, "^Guest baseline: invalid result$"):
            self.rpc(source)

    def test_duplex_preserves_both_streams_and_large_input(self):
        source = (
            "import sys; sys.stdout.buffer.write(b'o'*32768); sys.stdout.flush();"
            "sys.stderr.buffer.write(b'e'*32768); sys.stderr.flush();"
            "assert len(sys.stdin.buffer.read()) == 262144"
        )
        result = self.run_child(
            source,
            lambda argv, timeout: guests.run_guest(argv, input="i" * 262144, timeout=timeout),
        )
        self.assertEqual(
            (result.returncode, len(result.stdout), len(result.stderr)), (0, 32768, 32768)
        )

    def test_overflow_while_input_is_blocked(self):
        with self.assertRaisesRegex(ValidationError, "response exceeds limit"):
            self.run_child(
                "import os,time; os.write(2,b'x'*65537); time.sleep(30)",
                lambda argv, timeout: guests.run_guest(argv, input="i" * 262144, timeout=timeout),
            )

    def test_broken_stdin_still_reads_response(self):
        result = self.run_child(
            "import os; os.close(0); os.write(1,b'{}')",
            lambda argv, timeout: guests.run_guest(argv, input="i" * 262144, timeout=timeout),
        )
        self.assertEqual((result.returncode, result.stdout), (0, "{}"))

    def test_timeout_reaps_blocked_child_including_after_output_eof(self):
        for source in (
            "import time; time.sleep(30)",
            "import os,time; os.close(1); os.close(2); time.sleep(30)",
        ):
            with self.subTest(source=source), self.assertRaises(subprocess.TimeoutExpired):
                self.run_child(
                    source,
                    lambda argv, timeout: guests.run_guest(argv, timeout=timeout),
                    timeout=0.2,
                )

    def test_overflow_cannot_be_reboot_disconnect(self):
        with self.assertRaisesRegex(ValidationError, "response exceeds limit"):
            self.rpc(
                "import os; os.write(2,b'x'*65537); raise SystemExit(255)",
                {"reboot_from": "12345678-1234-1234-1234-123456789abd"},
            )

    def test_readiness_overflow_fails_before_ready(self):
        with self.assertRaisesRegex(ValidationError, "response exceeds limit"):
            self.run_child(
                "import os; os.write(1,b'x'*65537)",
                lambda argv, timeout: guests.wait_guest(request(), Path("/unused"), False, None),
            )
