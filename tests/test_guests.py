"""Exercise selected guest admission, ownership, readiness and private transport."""

import contextlib
import copy
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
    host = managed_hosts(load_inventory(ROOT / "inventory/example.yml"))["ubuntu_local"]
    host["storage"] = "pool"
    host.pop("vlan", None)
    return guests.request_for(host, "a" * 40)


class TestGuestHost(unittest.TestCase):
    def setUp(self):
        self.request = request()
        self.host = self.request["host"]

    def test_capacity_checks_observed_load_and_whole_batch(self):
        node = {"cpuinfo": {"cpus": 4}, "cpu": 0.1, "loadavg": ["1.1", "0", "0"]}
        memory = "MemAvailable: 8388608 kB\n"
        storage = {"avail": 100 * 1024**3, "extent_bytes": 0}
        guest_host.check_capacity([self.request], node, memory, {"pool": storage})
        guest_host.check_capacity([], {}, "", {})
        for changed in [
            dict(node, cpu=None),
            dict(node, cpu=1.1),
            dict(node, loadavg=[]),
            dict(node, loadavg=["nan"]),
            dict(node, loadavg=["3"]),
        ]:
            with self.subTest(node=changed), self.assertRaises(guest_host.GuestError):
                guest_host.check_capacity([self.request], changed, memory, {"pool": storage})
        with self.assertRaises(guest_host.GuestError):
            guest_host.check_capacity([self.request, self.request], node, memory, {"pool": storage})
        for mem in ["MemAvailable: 1 kB\n", "MemFree: 8388608 kB\n"]:
            with self.assertRaises(guest_host.GuestError):
                guest_host.check_capacity([self.request], node, mem, {"pool": storage})
        with self.assertRaises(guest_host.GuestError):
            guest_host.check_capacity(
                [self.request], node, memory, {"pool": dict(storage, avail=32 * 1024**3)}
            )

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
        ]:
            other = copy.deepcopy(self.request)
            other["host"][key] = value
            self.assertNotEqual(guest_host.identity(other), identity)

    def test_acknowledgement_cannot_mark_another_guest_ready(self):
        expected = {
            "vmid": self.host["vmid"],
            "identity": guest_host.identity(self.request),
            "verified": True,
        }
        for wrong in [
            dict(expected, vmid=999),
            dict(expected, identity="b" * 64),
            dict(expected, verified=False),
            dict(expected, extra=1),
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
            ide2="pool:vm-1101-cloudinit,media=cdrom",
            efidisk0="pool:vm-1101-disk-1,efitype=4m,pre-enrolled-keys=1,size=4M",
        )
        config["ipconfig0"] = ",".join(reversed(config["ipconfig0"].split(",")))
        guest_host.check_configuration(self.request, config, [], "ready")
        for key, value in [
            ("onboot", 1),
            ("memory", 2),
            ("scsi1", "foreign:disk"),
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


class TestGuestVerifier(unittest.TestCase):
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
        with patch.object(guest_verify.Path, "read_text", return_value="1"):
            self.assertEqual(guest_verify.security_state("opensuse"), "selinux-enforcing")
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
        observed = {
            "os_id": "ubuntu",
            "release": "24.04",
            "architecture": "x86_64",
            "hostname": "ubuntu-local",
            "fqdn": req["host"]["fqdn"],
            "ipv4": [req["host"]["ansible_host"]],
            "cpus": 2,
            "memory_bytes": 4 * 1024**3,
            "filesystem_bytes": 31 * 1024**3,
            "security": "apparmor-enforcing",
            "kvm_api": 12,
            "kvm_create_vm": True,
        }
        guest_verify.validate_observation(req, observed)
        cases = {
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


class GuestNativeFixture:
    """Native command boundary with persistent template/guest configuration and disks."""

    def __init__(self, req):
        self.template = NativeFixture(req["template"])
        self.request = req
        self.guests = {}
        self.volumes = []
        self.calls = []
        self.fail_clone = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
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
            for slot in ("scsi0", "ide2", "efidisk0"):
                config[slot] = (
                    config[slot].replace("base-9001", f"vm-{vmid}").replace("vm-9001", f"vm-{vmid}")
                )
            self.guests[vmid] = {"config": config, "status": "stopped", "pending": []}
            for v in self.template.volumes:
                self.volumes.append(
                    dict(
                        v,
                        vmid=vmid,
                        volid=v["volid"]
                        .replace("base-9001", f"vm-{vmid}")
                        .replace("vm-9001", f"vm-{vmid}"),
                    )
                )
            return ""
        vmid = int(argv[2])
        if vmid == 9001:
            return self.template(argv, **kwargs)
        guest = self.guests[vmid]
        if argv[:2] == ["qm", "set"]:
            opts = dict(zip(argv[3::2], argv[4::2], strict=True))
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
        for vmid, guest in self.guests.items():
            prefix = f"/nodes/{self.template.node}/qemu/{vmid}/"
            if path.startswith(prefix):
                kind = path.removeprefix(prefix)
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
        if ack is None:
            ack = {"vmid": 1101, "identity": guest_host.identity(self.req), "verified": True}
        output = io.StringIO()
        with (
            patch.object(guest_host, "read_line", return_value=ack),
            contextlib.redirect_stdout(output),
        ):
            guest_host.session([self.req], mode)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_fresh_ready_rerun_and_selected_drift_preserve_resources(self):
        events = self.execute("plan")
        self.assertEqual(events[0]["action"], "would-create")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))
        self.assertFalse((Path(self.temp) / "locks").exists())
        events = self.execute("apply")
        self.assertEqual(events[-1]["action"], "created")
        self.assertTrue(self.fixture.guests[1101]["config"]["description"].endswith(":ready"))
        writes = [c[1] for c in self.fixture.calls if c[0] == "qm"]
        self.assertEqual(writes, ["clone", "set", "resize", "start", "agent", "set"])
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
                "apply", {"vmid": 1102, "identity": guest_host.identity(self.req), "verified": True}
            )
        self.assertTrue(self.fixture.guests[1101]["config"]["description"].endswith(":preparing"))
        self.fixture.calls.clear()
        with self.assertRaises(guest_host.GuestError):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_slow_successful_guest_can_acknowledge_within_controller_budget(self):
        reader, writer = os.pipe()
        ack = {"vmid": 1101, "identity": guest_host.identity(self.req), "verified": True}
        os.write(writer, (json.dumps(ack) + "\n").encode())
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
                    "ubuntu_local",
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
        }
        guests.validate_event(req, result, "prepared")
        with self.assertRaises(ValidationError):
            guests.validate_event(req, dict(result, identity="b" * 64), "prepared")
        with self.assertRaises(ValidationError):
            guests.validate_event(req, dict(result, fqdn="secret.example.invalid"), "prepared")
