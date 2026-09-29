"""Observe snapshot, reset and deletion contracts at native/controller boundaries."""

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import guest_host, guests
from tests import test_guests


class TestSnapshots(unittest.TestCase):
    setUp = test_guests.TestNativeLifecycle.setUp

    def execute(self, mode, **kwargs):
        output = io.StringIO()

        def acknowledge(timeout):
            event = json.loads(output.getvalue().splitlines()[-1])
            return {
                "vmid": event["vmid"],
                "identity": event["identity"],
                "verified": True,
                "phase": "post-reboot" if event["phase"] == "reboot" else event["phase"],
            }

        with (
            patch.object(guest_host, "read_line", side_effect=acknowledge),
            contextlib.redirect_stdout(output),
        ):
            guest_host.session([self.req], mode, **kwargs)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_fresh_snapshot_and_rerun(self):
        events = self.execute("apply")
        snap = copy.deepcopy(self.fixture.snapshots[1101]["clean"])
        calls = [c for c in self.fixture.calls if c[0] == "qm"]
        capture = next(c for c in calls if c[1] == "snapshot")
        self.assertEqual(capture[capture.index("--vmstate") + 1], "0")
        self.assertLess(
            [c[1] for c in calls].index("shutdown"), [c[1] for c in calls].index("snapshot")
        )
        self.assertEqual(events[-2]["phase"], "baseline-boot")
        self.assertEqual(events[-1]["snapshot"], "clean")
        self.fixture.disk_state[1101] = {"/test": "used"}
        self.fixture.calls.clear()
        self.assertEqual(self.execute("apply")[-1]["action"], "preserved")
        self.assertEqual(self.fixture.snapshots[1101]["clean"], snap)
        self.assertEqual(self.fixture.disk_state[1101], {"/test": "used"})
        self.assertEqual([c[1] for c in self.fixture.calls if c[0] == "qm"], ["agent"])

    def test_restore_twice(self):
        self.execute("apply")
        snap = copy.deepcopy(self.fixture.snapshots[1101]["clean"])
        for cycle in range(2):
            self.fixture.disk_state[1101] = {"/test": str(cycle), "/etc/test": "changed"}
            result = self.execute("restore", confirmed=True, exclusive=True)
            self.assertEqual(result[-1]["action"], "restored")
            self.assertEqual(self.fixture.disk_state[1101], {})
            self.assertEqual(self.fixture.snapshots[1101]["clean"], snap)
            self.assertFalse(result[0]["fresh"])
        self.fixture.guests[1101]["status"] = "stopped"
        self.assertEqual(
            self.execute("restore", confirmed=True, exclusive=True)[-1]["action"], "restored"
        )

    def test_invalid_baseline_refused(self):
        self.execute("apply")
        original = copy.deepcopy(self.fixture.snapshots)
        for fault in ("missing", "identity", "ram", "hash", "partial", "disk", "generation"):
            with self.subTest(fault=fault):
                self.fixture.snapshots = copy.deepcopy(original)
                snap = self.fixture.snapshots[1101]["clean"]
                if fault == "missing":
                    del self.fixture.snapshots[1101]["clean"]
                elif fault == "ram":
                    snap["vmstate"] = 1
                elif fault == "partial":
                    snap["config"]["snapstate"] = "prepare"
                elif fault == "disk":
                    snap["config"]["scsi0"] = "foreign:vm-999-disk-0,size=32G"
                elif fault == "generation":
                    snap["config"]["vmgenid"] = "not-a-uuid"
                else:
                    value = json.loads(snap["description"])
                    value["identity" if fault == "identity" else "config_sha256"] = "0" * 64
                    snap["description"] = json.dumps(value)
                self.fixture.calls.clear()
                with self.assertRaises(ValueError):
                    self.execute("restore", confirmed=True, exclusive=True)
                self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def seed_file(self):
        config = self.fixture.guests[1101]["config"]
        name = config["cicustom"].rsplit("/", 1)[1]
        return self.fixture.snippets / name

    def test_clone_writes_owned_network_seed(self):
        self.execute("apply")
        config = self.fixture.guests[1101]["config"]
        volume, name, content = guest_host.seed_volume(self.req, config)
        self.assertEqual(config["cicustom"], "network=" + volume)
        path = self.fixture.snippets / name
        info = path.lstat()
        self.assertEqual((info.st_mode & 0o777, info.st_nlink), (0o600, 1))
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(sorted(p.name for p in self.fixture.snippets.iterdir()), [name])
        calls = self.fixture.calls
        path_call = calls.index(["pvesm", "path", volume])
        first_set = next(i for i, c in enumerate(calls) if c[:2] == ["qm", "set"])
        self.assertLess(path_call, first_set)
        self.assertIn("--cicustom", calls[first_set])
        for snapshot in self.fixture.snapshots[1101].values():
            self.assertEqual(snapshot["config"]["cicustom"], config["cicustom"])

    def test_existing_seed_is_reused_only_when_identical_and_safe(self):
        self.execute("apply")
        path = self.seed_file()
        content = path.read_bytes()
        self.execute("teardown", confirmed=True, exclusive=True)
        self.assertFalse(path.exists())
        path.write_bytes(content)
        path.chmod(0o600)
        self.assertEqual(self.execute("apply")[-1]["action"], "created")
        self.assertEqual(path.read_bytes(), content)
        self.execute("teardown", confirmed=True, exclusive=True)
        for fault in ("bytes", "symlink", "hardlink", "writable"):
            with self.subTest(fault=fault):
                for item in self.fixture.snippets.iterdir():
                    item.unlink()
                if fault == "symlink":
                    target = self.fixture.snippets / "target"
                    target.write_bytes(content)
                    path.symlink_to(target)
                else:
                    path.write_bytes(b"other" if fault == "bytes" else content)
                    path.chmod(0o622 if fault == "writable" else 0o600)
                    if fault == "hardlink":
                        os.link(path, self.fixture.snippets / "second")
                self.fixture.calls.clear()
                with self.assertRaises((guest_host.GuestError, OSError)):
                    self.execute("apply")
                self.assertFalse(any(c[:2] == ["qm", "set"] for c in self.fixture.calls))
                self.assertTrue(path.is_symlink() or path.read_bytes() in {content, b"other"})
                del self.fixture.guests[1101]
                self.fixture.volumes = [v for v in self.fixture.volumes if v["vmid"] != 1101]

    def test_seed_drift_stops_verify_restore_and_start(self):
        self.execute("apply")
        path = self.seed_file()
        content = path.read_bytes()
        for fault in ("missing", "bytes", "reference"):
            with self.subTest(fault=fault):
                config = self.fixture.guests[1101]["config"]
                reference = config["cicustom"]
                if fault == "missing":
                    path.unlink()
                elif fault == "bytes":
                    path.write_bytes(content + b"# changed\n")
                else:
                    config["cicustom"] = reference.replace("kdive-net", "kdive-other")
                for mode, kwargs in (
                    ("verify", {}),
                    ("restore", {"confirmed": True, "exclusive": True}),
                ):
                    self.fixture.calls.clear()
                    with self.assertRaises(guest_host.GuestError):
                        self.execute(mode, **kwargs)
                    self.assertFalse(
                        any(
                            c[:2] in (["qm", "start"], ["qm", "rollback"])
                            for c in self.fixture.calls
                        )
                    )
                path.unlink(missing_ok=True)
                path.write_bytes(content)
                path.chmod(0o600)
                config["cicustom"] = reference
                self.assertEqual(self.execute("verify")[-1]["action"], "preserved")

    def test_rollback_seed_drift_stops_before_start(self):
        self.execute("apply")
        path = self.seed_file()
        original = guest_host.command

        def rollback_then_drift(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ["qm", "rollback"]:
                path.write_bytes(b"changed")
            return result

        with patch.object(guest_host, "command", side_effect=rollback_then_drift):
            self.fixture.calls.clear()
            with self.assertRaisesRegex(guest_host.GuestError, "Network seed"):
                self.execute("restore", confirmed=True, exclusive=True)
        self.assertIn(["qm", "rollback", "1101", "clean", "--start", "0"], self.fixture.calls)
        self.assertFalse(any(c[:2] == ["qm", "start"] for c in self.fixture.calls))

    def test_snippet_storage_admission(self):
        for field, value in (
            ("active", 0),
            ("enabled", 0),
            ("content", "iso,vztmpl"),
        ):
            with self.subTest(field=field):
                self.fixture.snippet_status = dict(self.fixture.snippet_status, **{field: value})
                self.fixture.calls.clear()
                with self.assertRaisesRegex(guest_host.GuestError, "Snippet storage"):
                    self.execute("apply")
                self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))
                self.fixture.snippet_status = {
                    "active": 1,
                    "enabled": 1,
                    "type": "dir",
                    "content": "iso,snippets",
                }

    def test_unsafe_snippet_directory_refuses_before_clone(self):
        for fault in ("writable", "absent", "symlink"):
            with self.subTest(fault=fault):
                directory = self.fixture.snippets
                if fault == "writable":
                    directory.chmod(0o775)
                elif fault == "absent":
                    directory.rmdir()
                else:
                    real = directory.with_name("real-snippets")
                    real.mkdir(mode=0o755)
                    directory.rmdir()
                    directory.symlink_to(real)
                self.fixture.calls.clear()
                with self.assertRaises((guest_host.GuestError, OSError)):
                    self.execute("apply")
                self.assertFalse(any(c[:2] == ["qm", "clone"] for c in self.fixture.calls))
                if fault == "symlink":
                    directory.unlink()
                    directory.with_name("real-snippets").rmdir()
                if not directory.exists():
                    directory.mkdir(mode=0o755)
                directory.chmod(0o755)

    def test_teardown_releases_seed_only_when_unreferenced(self):
        for case in (
            "referenced",
            "snapshot-reference",
            "incomplete",
            "changed",
            "missing",
            "clean",
        ):
            with self.subTest(case=case):
                self.execute("apply")
                path = self.seed_file()
                config = copy.deepcopy(self.fixture.guests[1101]["config"])
                if case == "referenced":
                    self.fixture.guests[1300] = {
                        "config": config,
                        "status": "stopped",
                        "pending": [],
                    }
                elif case == "snapshot-reference":
                    self.fixture.guests[1300] = {
                        "config": {"name": "other"},
                        "status": "stopped",
                        "pending": [],
                    }
                    self.fixture.snapshots[1300] = {"old": {"config": config}}
                elif case == "changed":
                    path.write_bytes(b"changed")
                elif case == "missing":
                    path.unlink()
                if case == "incomplete":
                    with (
                        patch.object(
                            guest_host, "PVE_NODES", self.fixture.nodes.with_name("absent")
                        ),
                        self.assertRaisesRegex(guest_host.GuestError, "incomplete"),
                    ):
                        self.execute("teardown", confirmed=True, exclusive=True)
                elif case in ("referenced", "snapshot-reference", "changed"):
                    with self.assertRaisesRegex(guest_host.GuestError, "retained"):
                        self.execute("teardown", confirmed=True, exclusive=True)
                else:
                    result = self.execute("teardown", confirmed=True, exclusive=True)
                    self.assertEqual(result[-1]["action"], "destroyed")
                self.assertNotIn(1101, self.fixture.guests)
                self.assertEqual(path.exists(), case not in ("missing", "clean"))
                self.fixture.guests.pop(1300, None)
                self.fixture.snapshots.pop(1300, None)
                path.unlink(missing_ok=True)

    def test_unreadable_seed_after_destroy_reports_removed_guest(self):
        for fault in ("symlink", "directory"):
            with self.subTest(fault=fault):
                self.execute("apply")
                path = self.seed_file()
                content = path.read_bytes()
                if fault == "symlink":
                    target = self.fixture.snippets / "target"
                    target.write_bytes(content)
                    path.unlink()
                    path.symlink_to(target)
                else:
                    shutil.rmtree(self.fixture.snippets)
                with self.assertRaisesRegex(
                    guest_host.GuestError, "^Guest removed; network seed retained"
                ):
                    self.execute("teardown", confirmed=True, exclusive=True)
                self.assertNotIn(1101, self.fixture.guests)
                if fault == "directory":
                    self.fixture.snippets.mkdir(mode=0o755)
                for item in self.fixture.snippets.iterdir():
                    item.unlink()

    def test_selected_teardown(self):
        self.execute("apply")
        template = copy.deepcopy(self.fixture.template.config)
        unselected = copy.deepcopy(self.fixture.guests[1101])
        unselected["config"].pop("cicustom")
        self.fixture.guests[1200] = unselected
        del self.fixture.snapshots[1101]["clean"]
        self.fixture.guests[1101]["config"].pop("parent")
        self.assertEqual(self.execute("plan-teardown")[-1]["action"], "would-destroy")
        result = self.execute("teardown", confirmed=True, exclusive=True)
        self.assertEqual(result[-1]["action"], "destroyed")
        self.assertNotIn(1101, self.fixture.guests)
        self.assertFalse(any(v["vmid"] == 1101 for v in self.fixture.volumes))
        self.assertEqual(self.fixture.guests[1200], unselected)
        self.assertEqual(self.fixture.template.config, template)
        self.assertEqual(
            self.execute("teardown", confirmed=True, exclusive=True)[-1]["action"], "absent"
        )

    def test_destructive_intent_required_before_native_access(self):
        for mode in ("restore", "teardown"):
            for flags in (
                {},
                {"confirmed": True},
                {"exclusive": True},
                {"confirmed": 1, "exclusive": True},
            ):
                with (
                    self.subTest(mode=mode, flags=flags),
                    patch.object(guest_host, "native") as native,
                ):
                    with self.assertRaises(guest_host.GuestError):
                        self.execute(mode, **flags)
                    native.assert_not_called()

    def test_partial_failure(self):
        self.execute("apply")
        command = self.fixture
        for fail in ("shutdown", "rollback", "start"):
            self.fixture.guests[1101]["status"] = "running"
            self.fixture.calls.clear()

            def fault(argv, fail=fail, **kwargs):
                if argv[:2] == ["qm", fail]:
                    raise guest_host.GuestError(
                        "Native command unavailable or timed out; inspect host task state"
                    )
                return command(argv, **kwargs)

            with patch.object(guest_host, "command", side_effect=fault):
                with self.assertRaises(guest_host.GuestError):
                    self.execute("restore", confirmed=True, exclusive=True)
            self.assertIn(1101, self.fixture.guests)
            self.assertIn("clean", self.fixture.snapshots[1101])
            self.assertFalse(any(c[:2] == ["qm", "destroy"] for c in self.fixture.calls))

    def test_busy_lock(self):
        self.execute("apply")
        with guest_host.template_host.template_lock(1101):
            for mode in ("verify", "restore", "teardown"):
                with self.subTest(mode=mode), self.assertRaisesRegex(guest_host.GuestError, "busy"):
                    self.execute(mode, confirmed=True, exclusive=True)

    def test_snapshot_failure_cannot_recapture_existing_guest(self):
        command = self.fixture

        def fail_snapshot(argv, **kwargs):
            if argv[:2] == ["qm", "snapshot"]:
                raise guest_host.GuestError("Native task timed out")
            return command(argv, **kwargs)

        with patch.object(guest_host, "command", side_effect=fail_snapshot):
            with self.assertRaisesRegex(guest_host.GuestError, "timed out"):
                self.execute("apply")
        self.assertEqual(self.fixture.guests[1101]["status"], "stopped")
        self.fixture.guests[1101]["status"] = "running"
        self.fixture.calls.clear()
        with self.assertRaisesRegex(guest_host.GuestError, "baseline missing"):
            self.execute("apply")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_teardown_refuses_foreign_snapshot_disks_and_ownership(self):
        self.execute("apply")
        original = copy.deepcopy(self.fixture.snapshots[1101]["clean"])
        self.fixture.snapshots[1101]["clean"]["config"]["scsi0"] = "pool:vm-999-disk-0"
        for fault in ("disk", "ownership", "extra-volume"):
            if fault == "ownership":
                self.fixture.snapshots[1101]["clean"] = original
                self.fixture.guests[1101]["config"]["description"] = "foreign"
            if fault == "extra-volume":
                self.fixture.guests[1101]["config"]["description"] = guest_host.marker(
                    self.req, "ready"
                )
                self.fixture.volumes.append(
                    dict(self.fixture.volumes[0], volid="pool:vm-1101-disk-99")
                )
            self.fixture.calls.clear()
            with self.subTest(fault=fault), self.assertRaises(guest_host.GuestError):
                self.execute("teardown", confirmed=True, exclusive=True)
            self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))

    def test_post_capture_ack_is_distinct_and_required(self):
        ack = {
            "vmid": 1101,
            "identity": guest_host.identity(self.req),
            "verified": True,
            "phase": "prepared",
        }
        with (
            patch.object(guest_host, "read_line", return_value=ack),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(guest_host.GuestError, "acknowledgement mismatch"):
                guest_host.session([self.req], "apply")
        self.assertIn("clean", self.fixture.snapshots[1101])


class TestSnapshotController(unittest.TestCase):
    def test_destructive_flags_before_remote(self):
        for operation in ("--restore", "--teardown"):
            for flags in ([], ["--confirm", "fedora"], ["--confirm", "ubuntu"], ["--exclusive"]):
                with (
                    self.subTest(operation=operation, flags=flags),
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(
                        guests.sys,
                        "argv",
                        ["guests.py", operation, "--targets", "ubuntu", "--apply", *flags],
                    ),
                    patch.object(guests.templates, "api_admission") as api,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(guests.main(), 1)
                    api.assert_not_called()

    def test_playbook_entrypoints(self):
        for name in ("restore", "teardown"):
            result = subprocess.run(
                [
                    str(test_guests.ROOT / ".venv/bin/ansible-playbook"),
                    "-i",
                    "localhost,",
                    str(test_guests.ROOT / "playbooks" / (name + ".yml")),
                    "--syntax-check",
                ],
                cwd=test_guests.ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_restore_exchange_uses_readonly_verification_and_boot_budget(self):
        req = test_guests.request()
        uuid = "12345678-1234-1234-1234-123456789abc"
        event = dict(
            vmid=req["host"]["vmid"],
            identity=guest_host.identity(req),
            phase="prepared",
            fresh=False,
            guest_uuid=uuid,
        )
        ready = {k: v for k, v in event.items() if k not in {"fresh", "guest_uuid"}} | {
            "phase": "ready",
            "action": "restored",
            "duration_seconds": 1,
            "config_sha256": "a" * 64,
            "snapshot": "clean",
            "snapshot_identity": "b" * 64,
            "snapshot_config_sha256": "c" * 64,
            "snapshot_time": 123456,
        }
        for wrong in (False, True):
            result = dict(ready, snapshot="other") if wrong else ready
            source = (
                "import sys,json;e=json.loads(sys.stdin.readline());"
                "assert e['confirmed'] and e['exclusive'];print("
                + repr(json.dumps(event))
                + ",flush=True);assert json.loads(sys.stdin.readline())['phase']=='prepared';print("
                + repr(json.dumps(result))
                + ",flush=True)"
            )
            with (
                self.subTest(wrong=wrong),
                patch.object(guests, "host_ssh", return_value=[sys.executable, "-u", "-c", source]),
                patch.object(guests, "verify_guest", return_value={"boot_id": uuid}) as verify,
            ):
                if wrong:
                    with self.assertRaises(ValueError):
                        guests.dispatch([req], "restore", Path("/unused"), True, True)
                else:
                    outcome = guests.dispatch([req], "restore", Path("/unused"), True, True)
                    self.assertEqual(outcome[0]["action"], "restored")
                    self.assertNotIn("boot_id", outcome[0])
                verify.assert_called_once_with(req, Path("/unused"), False, uuid, booting=True)

    def test_fresh_baseline_boot_binds_uuid_and_prior_boot(self):
        req = test_guests.request()
        uuid = "12345678-1234-1234-1234-123456789abc"
        prepared = dict(
            vmid=req["host"]["vmid"],
            identity=guest_host.identity(req),
            phase="prepared",
            fresh=True,
            guest_uuid=uuid,
        )
        boot = {k: v for k, v in prepared.items() if k != "fresh"} | {"phase": "baseline-boot"}
        ready = {k: v for k, v in prepared.items() if k not in {"fresh", "guest_uuid"}} | {
            "phase": "ready",
            "action": "created",
            "duration_seconds": 1,
            "config_sha256": "a" * 64,
            "snapshot": "clean",
            "snapshot_identity": "b" * 64,
            "snapshot_config_sha256": "c" * 64,
            "snapshot_time": 123456,
        }
        for wrong in (False, True):
            event = dict(boot, guest_uuid="12345678-1234-1234-1234-123456789abd") if wrong else boot
            source = (
                "import sys,json;json.loads(sys.stdin.readline());print("
                + repr(json.dumps(prepared))
                + ",flush=True);json.loads(sys.stdin.readline());print("
                + repr(json.dumps(event))
                + ",flush=True);assert json.loads(sys.stdin.readline())['phase']=="
                "'baseline-boot';print(" + repr(json.dumps(ready)) + ",flush=True)"
            )
            with (
                self.subTest(wrong=wrong),
                patch.object(guests, "host_ssh", return_value=[sys.executable, "-u", "-c", source]),
                patch.object(
                    guests, "verify_guest", side_effect=[{"boot_id": uuid}, {"boot_id": "new"}]
                ) as verify,
            ):
                if wrong:
                    with self.assertRaises(ValueError):
                        guests.dispatch([req], "apply", Path("/unused"))
                    self.assertEqual(verify.call_count, 1)
                else:
                    guests.dispatch([req], "apply", Path("/unused"))
                    self.assertEqual(verify.call_args.kwargs, {"after_boot": uuid})
