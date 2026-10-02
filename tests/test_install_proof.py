"""Exercise clean-only proof orchestration with process/native boundaries replaced."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import guest_host
from tests.test_guests import request


class NativeProofTests(unittest.TestCase):
    def test_proof_keeps_existing_locks_through_both_restores(self):
        req = request()
        active = set()
        events = []

        @contextlib.contextmanager
        def lock(vmid):
            active.add(vmid)
            yield
            active.remove(vmid)

        def restore(*args):
            self.assertEqual(active, {0, req["host"]["vmid"], req["host"]["template_vmid"]})
            events.append("restore")
            return {}

        def ack(timeout):
            self.assertGreater(timeout, 24 * 3600 + 30)
            events.append("proof")
            return {
                "vmid": req["host"]["vmid"],
                "identity": guest_host.identity(req),
                "phase": "proof",
                "verified": True,
            }

        with (
            patch.object(guest_host, "validate_request"),
            patch.object(guest_host, "admission", return_value=[True]),
            patch.object(guest_host.template_host, "template_lock", side_effect=lock),
            patch.object(guest_host, "snapshot_rows", return_value={"clean": {}}),
            patch.object(guest_host, "lifecycle", side_effect=restore),
            patch.object(guest_host, "baseline", return_value={}),
            patch.object(guest_host, "read_line", side_effect=ack),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            guest_host.session([req], "proof", confirmed=True, exclusive=True)
        self.assertEqual(events, ["restore", "proof", "restore"])
        self.assertEqual(active, set())
        self.assertEqual(
            [json.loads(s)["phase"] for s in out.getvalue().splitlines()],
            ["ready", "proof-reset", "ready"],
        )

    def test_initial_failure_and_invalid_ack_still_reset(self):
        req = request()
        for first, ack in [(guest_host.GuestError("verification"), {}), ({}, {})]:
            with (
                self.subTest(first=type(first)),
                patch.object(guest_host, "lifecycle", side_effect=[first, {}]) as restore,
                patch.object(guest_host, "snapshot_rows", return_value={"clean": {}}),
                patch.object(guest_host, "baseline", return_value={}),
                patch.object(guest_host, "read_line", return_value=ack),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                guest_host.proof_session(req)
            self.assertEqual(restore.call_count, 2)
            marker = [
                json.loads(s)
                for s in out.getvalue().splitlines()
                if json.loads(s)["phase"] == "proof-reset"
            ][0]
            self.assertFalse(marker["initial_ok"])

    def test_higher_snapshot_guard_precedes_mutation(self):
        with (
            patch.object(guest_host, "snapshot_rows", return_value={"clean": {}, "toolchain": {}}),
            patch.object(guest_host, "lifecycle") as restore,
            self.assertRaisesRegex(guest_host.GuestError, "only clean"),
        ):
            guest_host.proof_session(request())
        restore.assert_not_called()

    def test_broken_reset_marker_still_attempts_native_restore(self):
        with (
            patch.object(guest_host, "snapshot_rows", return_value={"clean": {}}),
            patch.object(guest_host, "lifecycle", return_value={}) as restore,
            patch.object(guest_host, "baseline", return_value={}),
            patch.object(guest_host, "read_line", return_value={}),
            patch.object(guest_host, "emit", side_effect=[None, BrokenPipeError(), None]),
            self.assertRaises(BrokenPipeError),
        ):
            guest_host.proof_session(request())
        self.assertEqual(restore.call_count, 2)

    def test_final_failure_is_not_swallowed(self):
        with (
            patch.object(guest_host, "snapshot_rows", return_value={"clean": {}}),
            patch.object(guest_host, "lifecycle", side_effect=[{}, guest_host.GuestError("reset")]),
            patch.object(guest_host, "baseline", return_value={}),
            patch.object(guest_host, "read_line", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(guest_host.GuestError, "reset"),
        ):
            guest_host.proof_session(request())

    def test_mutation_gates_and_one_guest(self):
        for confirmed, exclusive, requests in [
            (False, True, [request()]),
            (True, False, [request()]),
            (True, True, [request(), request()]),
        ]:
            with self.subTest(confirmed=confirmed, exclusive=exclusive, size=len(requests)):
                with patch.object(guest_host, "admission") as admit:
                    with self.assertRaises(guest_host.GuestError):
                        guest_host.session(requests, "proof", confirmed, exclusive)
                    admit.assert_not_called()


class ControllerProofTests(unittest.TestCase):
    def test_exit_status_preserves_runner_failure_and_separates_reset(self):
        from scripts import install_proof

        for code in [0, 1, 2, 3, 130, 143]:
            self.assertEqual(install_proof.exit_status(code, True), code)
            self.assertEqual(install_proof.exit_status(code, False), code or 1)
        self.assertEqual(install_proof.exit_status(None, True), 1)

    def test_cli_requires_one_alias_and_exact_destructive_gates(self):
        from scripts import install_proof

        for argv in [[], ["--targets", "ubuntu,fedora"], ["--targets", "ubuntu", "--apply"]]:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(install_proof.main(argv), 0)

    def test_runner_timeout_is_less_than_native_ack_timeout(self):
        from scripts import install_proof

        self.assertLess(install_proof.RUN_TIMEOUT + 30, 24 * 3600 + 120)


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        from scripts import install_proof

        self.module = install_proof
        self.req = request()
        self.base = {"vmid": self.req["host"]["vmid"], "identity": guest_host.identity(self.req)}
        self.prepared = self.base | {
            "phase": "prepared",
            "fresh": False,
            "guest_uuid": "12345678-1234-1234-1234-123456789abc",
        }
        self.ready = self.base | {
            "phase": "ready",
            "action": "restored",
            "config_sha256": "a" * 64,
            "duration_seconds": 1,
            "snapshot": "clean",
            "snapshot_identity": "b" * 64,
            "snapshot_config_sha256": "c" * 64,
            "snapshot_time": 1,
        }
        self.marker = self.base | {"phase": "proof-reset", "initial_ok": True}
        self.record = {
            "initial": None,
            "final": None,
            "runner_exit_code": None,
            "reset_verified": False,
        }
        self.process = SimpleNamespace(stdin=io.BytesIO())

    def exchange(self, events, runner, verification=None):
        with (
            patch.object(self.module.guests, "read_event", side_effect=events),
            patch.object(self.module.guests, "verify_guest", side_effect=verification),
        ):
            self.module.exchange(self.process, self.req, Path("/pins"), runner, self.record)

    def test_runner_error_still_verifies_final_restore(self):
        runner = Mock(side_effect=OSError("private runner path"))
        self.exchange([self.prepared, self.ready, self.marker, self.prepared, self.ready], runner)
        self.assertTrue(self.record["reset_verified"])
        self.assertIn("orchestration_error", self.record)
        self.assertNotIn("private", json.dumps(self.record["orchestration_error"]))

    def test_both_restores_and_proof_exit_are_retained(self):
        for code in (0, 1, 2, 3, 130, 143):
            with self.subTest(code=code):
                self.exchange(
                    [self.prepared, self.ready, self.marker, self.prepared, self.ready],
                    Mock(return_value=code),
                )
                self.assertEqual(self.record["runner_exit_code"], code)
                self.assertTrue(self.record["reset_verified"])
                self.assertEqual(self.record["initial"]["snapshot_identity"], "b" * 64)

    def test_failed_initial_verification_skips_runner_and_resets(self):
        runner = Mock()
        self.exchange(
            [self.prepared, self.marker | {"initial_ok": False}, self.prepared, self.ready],
            runner,
            [self.module.ValidationError("failed baseline"), None],
        )
        runner.assert_not_called()
        self.assertTrue(self.record["reset_verified"])
        self.assertIsNone(self.record["initial"])
        acks = [json.loads(s) for s in self.process.stdin.getvalue().splitlines()]
        self.assertEqual([a["verified"] for a in acks], [False, True])

    def test_final_verification_failure_cannot_report_reset(self):
        with self.assertRaises(self.module.ValidationError):
            self.exchange(
                [
                    self.prepared,
                    self.ready,
                    self.marker,
                    self.prepared,
                    self.module.ValidationError("native reset failed"),
                ],
                Mock(return_value=3),
                [None, self.module.ValidationError("reset baseline")],
            )
        self.assertFalse(self.record["reset_verified"])
        self.assertEqual(self.record["runner_exit_code"], 3)

    def test_changed_snapshot_is_not_a_verified_reset(self):
        with self.assertRaisesRegex(self.module.ValidationError, "changed"):
            self.exchange(
                [
                    self.prepared,
                    self.ready,
                    self.marker,
                    self.prepared,
                    self.ready | {"snapshot_identity": "d" * 64},
                ],
                Mock(return_value=0),
            )
        self.assertFalse(self.record["reset_verified"])

    def test_reset_marker_never_starts_runner(self):
        runner = Mock()
        self.exchange([self.marker | {"initial_ok": False}, self.prepared, self.ready], runner)
        runner.assert_not_called()
        self.assertTrue(self.record["reset_verified"])

    def test_apply_retains_unmodified_upstream_evidence_and_bound_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "reports" / "run"
            key = root / "key"
            key.write_text("private key placeholder")
            self.req["host"]["ansible_ssh_private_key_file"] = str(key)
            pins = root / "known_hosts"
            pins.write_text("")
            process = Mock()
            process.stdin, process.stdout = io.BytesIO(), io.BytesIO()
            process.__enter__ = Mock(return_value=process)
            process.__exit__ = Mock(return_value=False)
            process.poll.return_value = 0
            process.wait.return_value = 0
            operator = b"echo operator-owned\n"

            def run(argv, checkout, env, directory, interrupts):
                self.assertEqual(argv[argv.index("--candidate") + 1], "a" * 40)
                script = Path(argv[argv.index("--operator-prerequisites") + 1])
                self.assertEqual(script.read_bytes(), operator)
                self.assertTrue(env["PATH"].startswith(str(output / "ssh")))
                upstream = directory / "upstream"
                upstream.mkdir()
                (upstream / "result.json").write_text('[{"outcome":"blocked"}]')
                return 3

            with (
                patch.object(self.module.subprocess, "Popen", return_value=process),
                patch.object(self.module, "run_runner", side_effect=run),
                patch.object(
                    self.module.guests,
                    "read_event",
                    side_effect=[self.prepared, self.ready, self.marker, self.prepared, self.ready],
                ),
                patch.object(self.module.guests, "verify_guest"),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                result = self.module.apply(
                    (self.req, pins, root, root, output, "a" * 40, operator),
                    SimpleNamespace(guest_image="rocky-kdive-ready-10"),
                )
            self.assertEqual(result, 3)
            record = json.loads((output / "provenance.json").read_text())
            self.assertEqual(record["runner_exit_code"], 3)
            self.assertTrue(record["reset_verified"])
            self.assertEqual(record["initial"]["snapshot_identity"], "b" * 64)
            self.assertEqual(
                record["evidence_sha256"]["upstream/result.json"],
                self.module.digest_file(output / "upstream/result.json"),
            )
            self.assertEqual((output / "provenance.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertNotIn(self.req["host"]["ansible_host"], stdout.getvalue())
            self.assertNotIn(str(root), stdout.getvalue())


class TransportTests(unittest.TestCase):
    def test_inventory_identity_and_pins_survive_spaces_and_metacharacters(self):
        from scripts import install_proof

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key with space;%"
            key.write_text("test-key")
            pins = root / "known hosts"
            pins.write_text("")
            env = install_proof.ssh_environment(
                {"ansible_ssh_private_key_file": str(key)}, pins, root / "adapter"
            )
            result = subprocess.run(
                [str(root / "adapter/ssh"), "-G", "example.invalid"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("identitiesonly yes", result.stdout)
            self.assertIn("stricthostkeychecking true", result.stdout)
            self.assertIn("identityfile " + str(key).replace("%", "%%"), result.stdout)
            self.assertEqual((root / "adapter/config").stat().st_mode & 0o777, 0o600)

    def test_runner_exit_and_timeout_use_real_processes(self):
        from scripts import install_proof

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = install_proof.run_runner(
                [sys.executable, "-c", "raise SystemExit(3)"],
                root,
                os.environ.copy(),
                root,
                install_proof.Interrupts(),
            )
            self.assertEqual(code, 3)
            with patch.object(install_proof, "RUN_TIMEOUT", 0.1):
                code = install_proof.run_runner(
                    [sys.executable, "-c", "import time;time.sleep(60)"],
                    root,
                    os.environ.copy(),
                    root,
                    install_proof.Interrupts(),
                )
            self.assertEqual(code, 124)

    def test_signal_interrupt_stops_runner_before_return(self):
        from scripts import install_proof

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = "import os,signal,time;os.kill(os.getppid(),signal.SIGTERM);time.sleep(60)"
            with install_proof.Interrupts().installed() as interrupts:
                code = install_proof.run_runner(
                    [sys.executable, "-c", script], root, os.environ.copy(), root, interrupts
                )
            self.assertEqual(code, 143)
            self.assertEqual(interrupts.signum, signal.SIGTERM)


class PreflightTests(unittest.TestCase):
    def test_source_drift_and_output_escape_fail_before_api_or_mutation(self):
        from scripts import install_proof

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory = root / "lab.yml"
            inventory.write_bytes((install_proof.ROOT / "inventory/example.yml").read_bytes())
            (root / "known_hosts").touch(mode=0o600)
            bundle = root / "bundle"
            bundle.mkdir()
            for name in ("manifest.json", "effective_config", "kernel.tar.gz"):
                (bundle / name).write_text("test")
            args = SimpleNamespace(
                targets="ubuntu",
                apply=False,
                confirm=None,
                exclusive=False,
                inventory=str(inventory),
                kdive_checkout=str(root),
                kernel_bundle=str(bundle),
                guest_image="fedora-kdive-ready-44",
                output=str(root / "public-output"),
                operator_prerequisites=None,
            )
            pin = json.loads((install_proof.ROOT / "vars/kdive-source.json").read_text())["commit"]
            real_run = subprocess.run

            def external_run(argv, **kwargs):
                if argv[0] == str(root / ".venv/bin/python"):
                    return subprocess.CompletedProcess(argv, 0)
                return real_run(argv, **kwargs)

            for head, dirty, expected in [
                ("0" * 40, "", "pinned HEAD"),
                (pin, " M tracked", "pinned HEAD"),
                (pin, "", "ignored reports"),
            ]:
                with (
                    self.subTest(head=head, dirty=dirty),
                    patch.object(
                        install_proof.subprocess, "check_output", side_effect=[head, dirty]
                    ),
                    patch.object(install_proof.subprocess, "run", side_effect=external_run),
                    patch.object(install_proof.templates, "api_admission") as api,
                    self.assertRaisesRegex(install_proof.ValidationError, expected),
                ):
                    install_proof.preflight(args)
                api.assert_not_called()
            self.assertFalse((root / "public-output").exists())

    def test_evidence_digest_rejects_symlink(self):
        from scripts import install_proof

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file").write_bytes(b"evidence")
            (root / "link").symlink_to(root / "file")
            self.assertEqual(len(install_proof.digest_file(root / "file")), 64)
            with self.assertRaises(install_proof.ValidationError):
                install_proof.digest_file(root / "link")
