"""Named level contracts at native, guest filesystem and controller boundaries."""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import guest_host, guest_verify, guests
from tests import test_snapshots


def entry(name="tools", parent="clean"):
    return {"name": name, "parent": parent, "prepare": lambda req: {}, "check": lambda req, c: None}


class TestLevelMetadata(unittest.TestCase):
    def test_closed_registry_and_exact_metadata(self):
        with patch.object(guest_verify, "LEVELS", (entry(),)):
            self.assertEqual(guest_verify.level_chain("clean"), [])
            self.assertEqual(guest_verify.level_chain("tools")[0]["parent"], "clean")
            with self.assertRaises(ValueError):
                guest_verify.level_chain("../bad")
            parent = {"schema": 1, "identity": "a" * 64, "config_sha256": "b" * 64}
            metadata = dict(
                parent,
                level="tools",
                parent="clean",
                parent_identity=guest_host.template_host.digest(parent),
                content={},
            )
            guest_verify.level_metadata(metadata, "tools", parent, "a" * 64, "b" * 64)
            for field, value in (
                ("parent_identity", "0" * 64),
                ("schema", True),
                ("parent", "wrong"),
                ("content", []),
                ("extra", 1),
            ):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    guest_verify.level_metadata(
                        dict(metadata, **{field: value}), "tools", parent, "a" * 64, "b" * 64
                    )

    def test_duplicate_json_and_bounds(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}', " " * 65537):
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                guest_verify.level_json(value)


class TestLevelManifest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp) / "levels"
        self.enterContext(patch.object(guest_verify, "LEVEL_DIRECTORY", self.root))
        real_fstat = os.fstat

        def root_stat(fd):
            values = list(real_fstat(fd))
            values[4] = 0
            return os.stat_result(values)

        self.enterContext(patch.object(guest_verify.os, "fstat", side_effect=root_stat))

    def test_roundtrip_and_mismatch(self):
        value = {"schema": 1}
        guest_verify.level_manifest("tools", value, write=True)
        self.assertEqual((self.root / "tools.json").stat().st_mode & 0o777, 0o644)
        guest_verify.level_manifest("tools", value)
        with self.assertRaises(ValueError):
            guest_verify.level_manifest("tools", {"schema": 2})
        with self.assertRaises(ValueError):
            guest_verify.level_manifest("tools", {"schema": 2}, write=True)

    def test_hook_prepare_manifest_verify_and_failed_check(self):
        parent = {"schema": 1, "identity": "a" * 64, "config_sha256": "b" * 64}
        proposed = dict(
            parent,
            level="tools",
            parent="clean",
            parent_identity=guest_host.template_host.digest(parent),
            content={},
        )
        marker = Path(self.temp) / "marker"

        def prepare(request):
            marker.write_text("ready")
            return {"marker": "ready"}

        def check(request, content):
            guest_verify.check(marker.read_text() == content["marker"], "Marker differs")

        item = dict(entry(), prepare=prepare, check=check)
        with (
            patch.object(guest_verify, "LEVELS", (item,)),
            patch.object(guest_verify, "run") as baseline,
        ):
            result = guest_verify.run_level({}, "prepare", "tools", [parent], proposed)
            self.assertEqual(baseline.call_count, 2)
            chain = [parent, result["metadata"]]
            self.assertEqual(
                guest_verify.run_level({}, "verify", "tools", chain, None), {"verified": True}
            )
            marker.write_text("damaged")
            with self.assertRaisesRegex(ValueError, "Marker differs"):
                guest_verify.run_level({}, "verify", "tools", chain, None)

    def test_symlink_and_unsafe_modes_refused(self):
        self.root.mkdir()
        target = Path(self.temp) / "other"
        target.write_text("{}")
        file = self.root / "tools.json"
        file.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            guest_verify.level_manifest("tools", {}, write=True)
        self.assertEqual(target.read_text(), "{}")
        file.unlink()
        guest_verify.level_manifest("tools", {}, write=True)
        file.chmod(0o666)
        with self.assertRaises(ValueError):
            guest_verify.level_manifest("tools", {})
        self.root.chmod(0o777)
        with self.assertRaises(ValueError):
            guest_verify.level_manifest("tools", {})


class TestLevelSnapshots(unittest.TestCase):
    execute = test_snapshots.TestSnapshots.execute

    def setUp(self):
        test_snapshots.TestSnapshots.setUp(self)
        self.stack.enter_context(
            patch.object(guest_verify, "LEVELS", (entry(), entry("source", "tools")))
        )
        self.execute("apply")

    def capture(self, name, parent):
        rows = self.fixture.snapshots[1101]
        metadata = json.loads(rows[parent]["description"])
        value = {
            "schema": 1,
            "level": name,
            "parent": parent,
            "parent_identity": guest_host.template_host.digest(metadata),
            "identity": guest_host.identity(self.req),
            "config_sha256": metadata["config_sha256"],
            "content": {},
        }
        self.fixture(["qm", "snapshot", "1101", name, "--description", json.dumps(value)])
        rows[name]["parent"] = parent
        rows[name]["snaptime"] = rows[parent]["snaptime"] + 1
        return value

    def prepare_events(self, fault=None):
        output = io.StringIO()
        command = guest_host.command

        def acknowledge(timeout):
            event = json.loads(output.getvalue().splitlines()[-1])
            ack = {
                "vmid": event["vmid"],
                "identity": event["identity"],
                "verified": True,
                "phase": event["phase"],
            }
            if event["phase"] == "levels":
                if fault == "ack":
                    ack["verified"] = False
                ack["metadata"] = dict(event["proposed"], content={"marker": "ready"})
                if fault == "config":
                    self.fixture.guests[1101]["config"]["cores"] = 9
            return ack

        def native(argv, **kwargs):
            if argv[:2] == ["qm", "shutdown"] and fault == "shutdown":
                raise guest_host.GuestError("shutdown failed")
            result = command(argv, **kwargs)
            if argv[:2] == ["qm", "shutdown"]:
                self.assertNotIn('"snapshot-ready"', output.getvalue())
            return result

        self.fixture.calls.clear()
        with (
            patch.object(guest_host, "read_line", side_effect=acknowledge),
            patch.object(guest_host, "command", side_effect=native),
            contextlib.redirect_stdout(output),
        ):
            if fault:
                with self.assertRaises(ValueError):
                    guest_host.session([self.req], "level", True, True, "tools")
            else:
                guest_host.session([self.req], "level", True, True, "tools")
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_ready_only_after_shutdown_never_captures(self):
        events = self.prepare_events()
        self.assertEqual(events[-1]["phase"], "snapshot-ready")
        self.assertEqual(self.fixture.guests[1101]["status"], "stopped")
        self.assertEqual(events[-1]["metadata"]["content"], {"marker": "ready"})
        self.assertFalse(any(c[:2] == ["qm", "snapshot"] for c in self.fixture.calls))
        self.assertNotIn("tools", self.fixture.snapshots[1101])

    def test_failed_preparation_never_ready(self):
        saved = copy.deepcopy(self.fixture.guests)
        for fault in ("ack", "config", "shutdown"):
            self.fixture.guests = copy.deepcopy(saved)
            with self.subTest(fault=fault):
                events = self.prepare_events(fault)
                self.assertFalse(any(e["phase"] == "snapshot-ready" for e in events))

    def test_prepare_plan_and_gates(self):
        self.fixture.calls.clear()
        events = self.execute("plan-level", level="tools")
        self.assertEqual(events[-1]["action"], "would-prepare-level")
        self.assertFalse(any(c[0] == "qm" for c in self.fixture.calls))
        for mode, level, flags in (
            ("level", "tools", {}),
            ("level", "clean", {"confirmed": True, "exclusive": True}),
            ("verify", "missing", {}),
        ):
            with (
                self.subTest(mode=mode, level=level),
                patch.object(guest_host, "native") as native,
                self.assertRaises(ValueError),
            ):
                guest_host.session([self.req], mode, level=level, **flags)
            native.assert_not_called()

    def test_chain_and_selected_rollback(self):
        self.capture("tools", "clean")
        self.capture("source", "tools")
        config = self.fixture.guests[1101]["config"]
        self.assertEqual(guest_host.baseline(self.req, config, "source")["snapshot"], "source")
        self.fixture.calls.clear()
        # Native restore does not read damaged guest manifests before rollback.
        with patch.object(guest_host, "verify_readiness", return_value=config):
            guest_host.lifecycle(self.req, "restore", "source")
        self.assertIn(["qm", "rollback", "1101", "source", "--start", "0"], self.fixture.calls)

    def test_wrong_parent_ram_missing_and_description_fail_closed(self):
        self.capture("tools", "clean")
        self.capture("source", "tools")
        saved = copy.deepcopy(self.fixture.snapshots)
        for fault in ("digest", "tree", "ram", "missing", "description"):
            self.fixture.snapshots = copy.deepcopy(saved)
            rows = self.fixture.snapshots[1101]
            if fault == "missing":
                del rows["tools"]
            elif fault == "tree":
                rows["source"]["parent"] = "clean"
            elif fault == "ram":
                rows["source"]["vmstate"] = 1
            elif fault == "digest":
                value = json.loads(rows["source"]["description"])
                value["parent_identity"] = "0" * 64
                rows["source"]["description"] = json.dumps(value)
            else:
                rows["source"]["config"]["scsi0"] = "foreign:wrong"
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                guest_host.baseline(self.req, self.fixture.guests[1101]["config"], "source")

    def test_zfs_newer_snapshot_refused_before_shutdown(self):
        self.capture("tools", "clean")
        self.fixture.calls.clear()
        with self.assertRaisesRegex(ValueError, "ZFS"):
            guest_host.lifecycle(self.req, "restore")
        self.assertFalse(any(c[:2] == ["qm", "shutdown"] for c in self.fixture.calls))


class TestLevelController(unittest.TestCase):
    def test_prepare_protocol_and_missing_guest_ack(self):
        from tests.test_guests import request

        req = request()
        uuid = "12345678-1234-1234-1234-123456789abc"
        base = {"vmid": req["host"]["vmid"], "identity": guest_host.identity(req)}
        clean = {"schema": 1, "identity": base["identity"], "config_sha256": "b" * 64}
        proposed = dict(
            clean,
            level="tools",
            parent="clean",
            parent_identity=guest_host.template_host.digest(clean),
            content={},
        )
        prepared = dict(base, phase="prepared", fresh=False, guest_uuid=uuid)
        levels = dict(
            base,
            phase="levels",
            guest_uuid=uuid,
            level="tools",
            prepare=True,
            chain=[clean],
            proposed=proposed,
        )
        ready = dict(base, phase="snapshot-ready", level="tools", metadata=proposed)
        source = (
            "import sys,json;json.loads(sys.stdin.readline());print("
            + repr(json.dumps(prepared))
            + ",flush=True);assert json.loads(sys.stdin.readline())['verified'];print("
            + repr(json.dumps(levels))
            + ",flush=True);assert json.loads(sys.stdin.readline())['metadata'];print("
            + repr(json.dumps(ready))
            + ",flush=True)"
        )
        with (
            patch.object(guest_verify, "LEVELS", (entry(),)),
            patch.object(guests, "host_ssh", return_value=[sys.executable, "-u", "-c", source]),
            patch.object(guests, "verify_guest", return_value={"boot_id": uuid}),
            patch.object(guests, "guest_rpc", return_value={"metadata": proposed}) as rpc,
        ):
            result = guests.dispatch([req], "level", Path("/unused"), True, True, "tools")
            self.assertEqual(result, [{"level": "tools", "metadata": proposed}])
            rpc.side_effect = ValueError("failed hook")
            with self.assertRaises(ValueError):
                guests.dispatch([req], "level", Path("/unused"), True, True, "tools")

    def test_cli_refuses_invalid_level_without_remote_access(self):
        for arguments in (
            ["--verify", "--level", "unknown"],
            ["--prepare-level"],
            ["--restore", "--level", "../escape"],
        ):
            env = dict(os.environ, TARGETS="ubuntu", LEVEL="clean")
            result = subprocess.run(
                [sys.executable, "scripts/guests.py", *arguments],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("level", result.stderr.lower())

    def test_expected_level_evidence(self):
        from tests.test_guests import request

        req = request()
        event = {
            "vmid": req["host"]["vmid"],
            "identity": guest_host.identity(req),
            "phase": "ready",
            "action": "preserved",
            "config_sha256": "a" * 64,
            "duration_seconds": 1,
            "snapshot": "tools",
            "snapshot_identity": "b" * 64,
            "snapshot_config_sha256": "c" * 64,
            "snapshot_time": 1,
        }
        with patch.object(guest_verify, "LEVELS", (entry(),)):
            guests.validate_event(req, event, "ready", "tools")
            with self.assertRaises(ValueError):
                guests.validate_event(req, event, "ready", "clean")
