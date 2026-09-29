"""Installed-level boundaries: real parsing, private execution and stop evidence."""

import ast
import contextlib
import copy
import inspect
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import guest_host
from scripts import guest_verify as g
from tests.test_guests import request

INPUTS = {"repo": "https://github.com/randomparity/kdive.git", "commit": "a" * 40}


class KdiveContractTests(unittest.TestCase):
    def test_inputs_registry_and_native_identity(self):
        g.kdive_inputs(INPUTS)
        req = request()
        identity = guest_host.identity(req)
        req["kdive_source"] = INPUTS
        guest_host.validate_request(req)
        self.assertEqual(guest_host.identity(req), identity)
        self.assertEqual(g.level_chain("kdive")[-1]["parent"], "kernel-src")
        for bad in (
            {},
            dict(INPUTS, commit="main"),
            dict(INPUTS, commit=True),
            dict(INPUTS, repo="https://user:password@example.org/repo"),
            dict(INPUTS, repo="file:///tmp/repo"),
            dict(INPUTS, extra=1),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                g.kdive_inputs(bad)

    def test_command_censors_failure_and_uses_stdin_sanitized_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            account = SimpleNamespace(pw_dir=temp)
            with (
                patch.object(g, "kdive_context", return_value=("operator", account, Path(temp))),
                patch.object(g, "KDIVE_STATE", Path(temp)),
                patch.object(g.subprocess, "run") as run,
            ):

                def fail(argv, **kwargs):
                    self.assertNotIn("secret-dsn", " ".join(argv))
                    self.assertIn("secret-dsn", kwargs["input"])
                    self.assertIn("-i", argv)
                    self.assertIn("DOCKER_HOST=unix:///var/run/docker.sock", argv)
                    self.assertIn("COMPOSE_PROJECT_NAME=kdive-level", argv)
                    kwargs["stderr"].write("secret-dsn private-upstream-error\n")
                    return SimpleNamespace(returncode=1)

                run.side_effect = fail
                with self.assertRaises(g.GuestError) as caught:
                    g.kdive_command({}, "setup", "export TEST=secret-dsn")
                self.assertIn("setup", str(caught.exception))
                self.assertNotIn("secret-dsn", str(caught.exception))
                logs = list(Path(temp).glob("*.log"))
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
                self.assertIn("secret-dsn", logs[0].read_text())

    def test_native_binding_credentials_are_exact_private_files(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "dsn"
            file.write_text(
                "postgresql://kdive-witness-member:kdive-witness-local@localhost:5432/kdive\n"
            )
            file.chmod(0o600)
            real = os.fstat

            def root_stat(fd):
                values = list(real(fd))
                values[4] = 0
                return os.stat_result(values)

            with (
                patch.object(g, "Path", return_value=file),
                patch.object(g.os, "fstat", side_effect=root_stat),
            ):
                g.kdive_witness()
                file.write_text("postgresql://shared.example/database\n")
                with self.assertRaisesRegex(g.GuestError, "guest-local"):
                    g.kdive_witness()
                file.chmod(0o644)
                with self.assertRaisesRegex(g.GuestError, "ownership"):
                    g.kdive_witness()

    def test_backend_rejects_remote_ports_volume_drivers_and_running_state(self):
        config = {
            "services": {
                name: {"ports": [{"host_ip": "127.0.0.1", "target": target}]}
                for name, target in (("postgres", 5432), ("seaweedfs", 8333), ("oidc", 8080))
            }
        }
        volume = {
            "Driver": "local",
            "Options": None,
            "Labels": {"com.docker.compose.project": "kdive-level"},
        }
        for fault in (None, "port", "driver", "options", "labels", "missing-volume", "running"):
            cfg, vol = copy.deepcopy(config), copy.deepcopy(volume)
            if fault == "port":
                cfg["services"]["postgres"]["ports"][0]["host_ip"] = "0.0.0.0"
            if fault == "driver":
                vol["Driver"] = "remote"
            if fault == "options":
                vol["Options"] = {"type": "nfs"}
            if fault == "labels":
                vol["Labels"] = {}

            def run(req, phase, script, cfg=cfg, vol=vol, fault=fault):
                if phase == "backend-config":
                    return json.dumps(cfg)
                if phase == "volumes":
                    return "kdive-level_kdive-pgdata\n" + (
                        "" if fault == "missing-volume" else "kdive-level_kdive-seaweedfs-data\n"
                    )
                if phase == "volume-inspect":
                    return json.dumps([vol])
                if phase == "containers":
                    return "abc" if fault == "running" else ""
                raise AssertionError(phase)

            with self.subTest(fault=fault), patch.object(g, "kdive_command", side_effect=run):
                if fault is None:
                    g.kdive_backend_check({})
                else:
                    with self.assertRaises(g.GuestError):
                        g.kdive_backend_check({})

    def test_stop_checks_host_daemons_even_after_successful_teardown(self):
        with (
            patch.object(g, "kdive_command") as run,
            patch.object(g, "command", return_value="inactive\n"),
        ):
            g.kdive_stopped({})
            self.assertIn("daemon_pids", run.call_args.args[2])
            run.side_effect = g.GuestError("surviving host daemon")
            with self.assertRaisesRegex(g.GuestError, "surviving"):
                g.kdive_stopped({})
        with patch.object(g, "kdive_command"), patch.object(g, "command", return_value="active\n"):
            with self.assertRaisesRegex(g.GuestError, "remains active"):
                g.kdive_stopped({})

    def test_stop_shell_rejects_survivor_with_successful_enumeration(self):
        scripts = []
        with (
            patch.object(
                g, "kdive_command", side_effect=lambda req, phase, script: scripts.append(script)
            ),
            patch.object(g, "command", return_value="inactive\n"),
        ):
            g.kdive_stopped({})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "scripts/live-stack"
            root.mkdir(parents=True)
            (root / "lib.sh").write_text("daemon_pids() { printf '12345\\n'; }\n")
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", scripts[0]], cwd=temp)
            self.assertNotEqual(result.returncode, 0)
            (root / "lib.sh").write_text("daemon_pids() { :; }\n")
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", scripts[0]], cwd=temp)
            self.assertEqual(result.returncode, 0)

    def test_checkout_rejects_wrong_revision_and_dirty_real_tree(self):
        from tests.test_kernel_source import KernelTreeTests

        fixture = KernelTreeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        inputs = dict(INPUTS, commit=fixture.commit)
        with patch.object(
            g, "kdive_context", return_value=("operator", fixture.account, fixture.dest)
        ):
            g.kdive_tree({}, inputs)
            with self.assertRaisesRegex(g.GuestError, "revision"):
                g.kdive_tree({}, dict(inputs, commit="f" * 40))
            (fixture.dest / "untracked").write_text("fault")
            with self.assertRaisesRegex(g.GuestError, "clean"):
                g.kdive_tree({}, inputs)

    def test_public_error_does_not_relay_installer_credentials(self):
        envelope = dict(request={}, level_operation="prepare", level="kdive", chain=[], proposed={})
        output = io.StringIO()
        with (
            patch.object(g.sys, "stdin", io.StringIO(json.dumps(envelope))),
            patch.object(g, "run_level", side_effect=g.GuestError("private-secret-dsn")),
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(g.main(), 1)
        self.assertEqual(output.getvalue(), g.KDIVE_DIAGNOSTIC + "\n")

    def test_admission_rejects_failed_and_nonempty_docker_enumeration(self):
        tree = ast.parse(inspect.getsource(g.prepare_kdive))
        call = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "kdive_command"
            and isinstance(n.args[1], ast.Constant)
            and n.args[1].value == "admission"
        )
        script = ast.literal_eval(call.args[2])
        with tempfile.TemporaryDirectory() as temp:
            for body in ("return 1", "printf existing", 'test "$1" != volume'):
                with self.subTest(body=body):
                    result = subprocess.run(
                        [
                            "bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            "docker() { " + body + "; }; uv() { :; }; " + script,
                        ],
                        cwd=temp,
                    )
                    self.assertNotEqual(result.returncode, 0)
