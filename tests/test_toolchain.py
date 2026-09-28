"""Toolchain capability, trust and filesystem boundary checks."""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import guest_verify as g


class ToolchainTests(unittest.TestCase):
    def test_registry_and_distribution_packages(self):
        self.assertEqual(g.level_chain("toolchain")[-1]["parent"], "clean")
        for profile, engine in (
            ("ubuntu", "docker.io"),
            ("fedora", "moby-engine"),
            ("rocky", "docker-ce"),
            ("opensuse", "docker"),
        ):
            packages = g.toolchain_packages(profile)
            self.assertIn(engine, packages)
            self.assertIn("git", packages)
            self.assertTrue(
                any("libvirt" in name and name.endswith(("dev", "devel")) for name in packages)
            )

    def test_operator_command_uses_fresh_login_and_quoted_arguments(self):
        with patch.object(g, "command", return_value="ok") as run:
            self.assertEqual(g.operator_command("operator", ["printf", "a; exit 7"]), "ok")
        self.assertEqual(
            run.call_args.args[0],
            [
                "runuser",
                "--login",
                "operator",
                "--shell",
                "/bin/bash",
                "--command",
                "printf 'a; exit 7'",
            ],
        )

    def test_failed_capability_names_operation_without_raw_output(self):
        with patch.object(g, "command", side_effect=g.GuestError("private endpoint")):
            with self.assertRaisesRegex(g.GuestError, "Docker access") as error:
                g.toolchain_command(["docker", "info"], "Docker access")
            self.assertNotIn("private endpoint", str(error.exception))

    def test_version_floor_and_missing_group(self):
        for version in ("4.4", "5.2"):
            g.toolchain_login_check(version, "docker kvm libvirt users")
        for version, groups, expected in (
            ("4.3", "docker kvm libvirt", "Bash"),
            ("5.2", "kvm libvirt", "docker"),
            ("5.2", "docker libvirt", "kvm"),
            ("5.2", "docker kvm", "libvirt"),
        ):
            with self.subTest(version=version, groups=groups):
                with self.assertRaisesRegex(g.GuestError, expected):
                    g.toolchain_login_check(version, groups)

    def test_package_digest_sorted_and_missing_headers_rejected(self):
        with patch.object(g, "command", return_value="b\na\n"):
            first = g.toolchain_package_hash("fedora")
        with patch.object(g, "command", return_value="a\nb\n"):
            self.assertEqual(first, g.toolchain_package_hash("fedora"))
        self.assertEqual(first, hashlib.sha256(b"a\nb\n").hexdigest())
        with patch.object(g, "command", return_value="installed\nconfig-files\n"):
            with self.assertRaisesRegex(g.GuestError, "required packages"):
                g.toolchain_check_packages("ubuntu")

    def test_observation_allows_added_packages_but_rejects_critical_drift(self):
        content = {
            "distro": "ubuntu",
            "release": "24.04",
            "uv": "0.12.19",
            "just": "1.58.0",
            "docker": "28.0.0",
            "libvirt": "10.0.0",
            "packages_sha256": "a" * 64,
        }
        with patch.object(
            g, "toolchain_observation", return_value=dict(content, packages_sha256="b" * 64)
        ):
            g.check_toolchain({}, content)
        for key in ("uv", "just", "docker", "libvirt", "release"):
            with (
                self.subTest(key=key),
                patch.object(
                    g, "toolchain_observation", return_value=dict(content, **{key: "different"})
                ),
            ):
                with self.assertRaisesRegex(g.GuestError, "differs"):
                    g.check_toolchain({}, content)
        with self.assertRaisesRegex(g.GuestError, "content"):
            g.check_toolchain({}, dict(content, extra=True))

    def test_source_paths_repair_only_owned_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir(mode=0o775)
            account = SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid(), pw_gid=os.getgid())
            real_lstat = Path.lstat

            def metadata(path):
                if path == home or path.is_relative_to(home):
                    return real_lstat(path)
                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)

            with patch.object(Path, "lstat", metadata):
                g.toolchain_source_path(account, prepare=True)
                self.assertEqual(home.stat().st_mode & 0o022, 0)
                self.assertEqual((home / "src").stat().st_uid, os.getuid())
                g.toolchain_source_path(account)
                (home / "src").chmod(0o777)
                with self.assertRaisesRegex(g.GuestError, "writable"):
                    g.toolchain_source_path(account)
                (home / "src").rmdir()
                (home / "src").symlink_to(home, target_is_directory=True)
                with self.assertRaisesRegex(g.GuestError, "symlink"):
                    g.toolchain_source_path(account, prepare=True)

    def test_observation_rejects_each_missing_tool_at_command_boundary(self):
        request = {
            "host": {"profile": "ubuntu", "ansible_user": "operator"},
            "template": {"image": {"release": "24.04"}},
        }
        outputs = {
            "bash": "5.2",
            "id": "docker kvm libvirt",
            "uv": "uv 0.12.19",
            "just": "just 1.58.0",
            "virsh": "10.0.0",
            "docker": "28.0.0",
        }
        missing = None

        def run(argv, timeout=60):
            if argv[0] == "runuser":
                import shlex

                tool = shlex.split(argv[-1])[0]
                if tool == missing:
                    raise g.GuestError("not found")
                return outputs.get(tool, "GNU available")
            if argv[0] == "dpkg-query":
                if "-f=${db:Status-Status}\n" == argv[2]:
                    return "installed\n" * len(g.toolchain_packages("ubuntu"))
                return "sample\t1\tinstalled\n"
            return "enabled"

        with (
            patch.object(g, "command", side_effect=run),
            patch.object(g, "toolchain_source_path"),
            patch.object(g.pwd, "getpwnam"),
            patch.object(g.shutil, "which", return_value="qemu-system-x86_64"),
        ):
            content = g.toolchain_observation(request)
            self.assertEqual(content["uv"], "0.12.19")
            for missing in (
                "git",
                "curl",
                "gcc",
                "make",
                "pkg-config",
                "python3",
                "shellcheck",
                "shfmt",
                "realpath",
                "find",
                "grep",
                "qemu-system-x86_64",
                "uv",
                "just",
                "docker",
                "virsh",
                "bash",
                "id",
            ):
                with self.subTest(tool=missing), self.assertRaises(g.GuestError):
                    g.toolchain_observation(request)

    def test_docker_key_fingerprint_exact(self):
        valid = "fpr:::::::::060A61C51B558A7F742B77AAC52FEB6B621E9F35:\n"
        g.check_docker_key(valid)
        for invalid in ("", valid.replace("060A", "FFFF"), valid + valid):
            with self.assertRaisesRegex(g.GuestError, "signing key"):
                g.check_docker_key(invalid)


if __name__ == "__main__":
    unittest.main()
