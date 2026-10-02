"""Installed-level boundaries: real parsing, private execution and stop evidence."""

import ast
import contextlib
import copy
import hashlib
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

    def test_command_includes_trusted_system_sbin_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            account = SimpleNamespace(pw_dir=temp)
            with (
                patch.object(g, "kdive_context", return_value=("operator", account, Path(temp))),
                patch.object(g, "KDIVE_STATE", Path(temp)),
                patch.dict(os.environ, {"PATH": "/untrusted/bin:.:"}),
                patch.object(
                    g.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                ) as run,
            ):
                g.kdive_command({}, "setup", "command -v ss tcpdump")
            argv = run.call_args.args[0]
            paths = next(value[5:] for value in argv if value.startswith("PATH=")).split(":")
            self.assertIn("-i", argv)
            self.assertEqual(
                paths[:4], [temp + "/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
            )
            self.assertTrue({"/usr/local/sbin", "/usr/sbin", "/sbin"}.issubset(paths))
            self.assertEqual(len(paths), 7)
            self.assertNotIn("/untrusted/bin", paths)
            self.assertTrue(all(path.startswith("/") for path in paths))

    def test_prepare_requires_genuine_fetched_main_to_match_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            req = request()
            req["kdive_source"] = INPUTS
            path = Path(temp)
            with (
                patch.object(g, "KDIVE_STATE", path / "absent-state"),
                patch.object(g, "kdive_context", return_value=("operator", None, path)),
                patch.object(g, "kdive_tree"),
                patch.object(g, "kernel_git", side_effect=["", "b" * 40]) as git,
                patch.object(g, "kdive_state") as state,
            ):
                with self.assertRaisesRegex(g.GuestError, "main differs"):
                    g.prepare_kdive(req)
                self.assertIn(
                    "refs/heads/main:refs/remotes/origin/main", git.call_args_list[0].args[1]
                )
                state.assert_not_called()

    def test_prepare_bootstraps_ansible_interpreter_before_host_play(self):
        for profile, manager in (("ubuntu", "apt-get"), ("fedora", "dnf"), ("rocky", "dnf")):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temp:
                req = request()
                req["host"]["profile"] = profile
                req["kdive_source"] = INPUTS
                path = Path(temp)
                with (
                    patch.object(g, "KDIVE_STATE", path / "absent-state"),
                    patch.object(g, "kdive_context", return_value=("operator", None, path)),
                    patch.object(g, "kdive_tree"),
                    patch.object(g, "kernel_git", return_value=INPUTS["commit"]),
                    patch.object(g, "kdive_state"),
                    patch.object(g, "kdive_configuration", return_value={"kernel_source": temp}),
                    patch.object(g, "kdive_backend_check"),
                    patch.object(g, "kdive_stopped"),
                    patch.object(g, "kdive_command") as run,
                ):
                    g.prepare_kdive(req)
                phases = [call.args[1] for call in run.call_args_list]
                index = phases.index("ansible-prerequisite")
                script = run.call_args_list[index].args[2]
                self.assertIn("sudo -n " + manager + " install", script)
                self.assertIn("python3-packaging", script)
                self.assertIn("/usr/bin/python3 -I -B -c 'import packaging'", script)
                self.assertLess(index, phases.index("host-install"))

    def test_installed_unit_checks_use_valid_instances(self):
        def systemctl(argv):
            self.assertFalse(any("@.service" in arg for arg in argv))
            return "enabled" if "is-enabled" in argv else "loaded"

        content = {
            "repo": INPUTS["repo"],
            "kdive_sha": INPUTS["commit"],
            "kernel_commit": "b" * 40,
            "playbook_inputs_sha256": hashlib.sha256(b"{}").hexdigest(),
        }
        with (
            patch.object(g, "kdive_tree"),
            patch.object(g, "kdive_context", return_value=("operator", None, Path("/tmp"))),
            patch.object(g, "kernel_path", return_value=Path("/tmp/kernel")),
            patch.object(g, "kernel_git", return_value=content["kernel_commit"]),
            patch.object(g, "kdive_configuration", return_value={}),
            patch.object(g, "kdive_state"),
            patch.object(g, "kdive_witness"),
            patch.object(g, "kdive_stopped"),
            patch.object(g, "kdive_command"),
            patch.object(g, "kdive_backend_check"),
            patch.object(g, "command", side_effect=systemctl),
        ):
            g.check_kdive({}, content)

    def test_replacing_either_ancestor_invalidates_native_kdive_binding(self):
        from tests.test_levels import TestLevelSnapshots

        levels = g.LEVELS
        fixture = TestLevelSnapshots()
        fixture.setUp()
        try:
            with patch.object(g, "LEVELS", levels):
                for name, parent in (
                    ("toolchain", "clean"),
                    ("kernel-src", "toolchain"),
                    ("kdive", "kernel-src"),
                ):
                    fixture.capture(name, parent)
                config = fixture.fixture.guests[1101]["config"]
                guest_host.baseline(fixture.req, config, "kdive")
                saved = copy.deepcopy(fixture.fixture.snapshots)
                for ancestor in ("toolchain", "kernel-src"):
                    fixture.fixture.snapshots = copy.deepcopy(saved)
                    row = fixture.fixture.snapshots[1101][ancestor]
                    metadata = json.loads(row["description"])
                    metadata["content"] = {"replacement": True}
                    row["description"] = json.dumps(metadata)
                    with (
                        self.subTest(ancestor=ancestor),
                        self.assertRaisesRegex(ValueError, "parent identity differs"),
                    ):
                        guest_host.baseline(fixture.req, config, "kdive")
        finally:
            fixture.doCleanups()

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
