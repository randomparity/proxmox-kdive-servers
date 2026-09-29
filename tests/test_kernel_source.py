"""Pinned source contracts using real Git trees and mocked external boundaries."""

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import guest_host, guests
from scripts import guest_verify as g
from tests.test_guests import request

REPO = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"


class KernelInputTests(unittest.TestCase):
    def test_inputs_and_request_identity(self):
        inputs = {"repo": REPO, "ref": "v6.9"}
        g.kernel_inputs(inputs)
        req = request()
        expected = guest_host.identity(req)
        req["kernel_source"] = inputs
        guest_host.validate_request(req)
        self.assertEqual(guest_host.identity(req), expected)
        for bad in ({}, dict(inputs, extra=True), dict(inputs, ref=True)):
            with self.subTest(bad=bad), self.assertRaises(g.GuestError):
                g.kernel_inputs(bad)
        for repo in (
            "/tmp/source",
            "http://example.org/a",
            "ssh://example.org/a",
            "ext::bad",
            "https://user:secret@example.org/a",
            REPO + "?x=y",
            REPO + "#ref",
            "https://example.org\n/a",
            "https://example.org:bad/a",
        ):
            with self.subTest(repo=repo), self.assertRaises(g.GuestError):
                g.kernel_inputs(dict(inputs, repo=repo))
        for ref in ("-bad", "*", "HEAD~1", "a..b", "a//b", "x.lock", "x\n", ""):
            with self.subTest(ref=ref), self.assertRaises(g.GuestError):
                g.kernel_inputs(dict(inputs, ref=ref))

    def test_ref_resolution_peels_tags_and_rejects_ambiguity(self):
        tag, commit = "a" * 40, "b" * 40
        with patch.object(
            g, "kernel_git", return_value=f"{tag}\trefs/tags/v6.9\n{commit}\trefs/tags/v6.9^{{}}\n"
        ):
            self.assertEqual(g.kernel_commit("operator", REPO, "v6.9"), commit)
        with patch.object(
            g, "kernel_git", return_value=f"{tag}\trefs/heads/v6.9\n{commit}\trefs/tags/v6.9\n"
        ):
            with self.assertRaisesRegex(g.GuestError, "ambiguous"):
                g.kernel_commit("operator", REPO, "v6.9")
        with patch.object(g, "kernel_git") as run:
            self.assertEqual(g.kernel_commit("operator", REPO, commit), commit)
            run.assert_not_called()
        for output in ("", "not-a-ref", f"{tag}\trefs/tags/other\n"):
            with patch.object(g, "kernel_git", return_value=output):
                with self.assertRaises(g.GuestError):
                    g.kernel_commit("operator", REPO, "v6.9")

    def test_controller_forwards_inputs_only_for_preparation(self):
        for operation in ("--prepare-level", "--verify", "--restore"):
            with (
                self.subTest(operation=operation),
                patch.object(
                    guests.sys,
                    "argv",
                    ["guests.py", operation, "--level", "kernel-src", "--targets", "ubuntu"],
                ),
                patch.object(guests, "validate_inventory"),
                patch.object(guests.templates, "api_admission", return_value={}),
                patch.object(guests, "request_for") as build_request,
                patch.object(guests, "dispatch", return_value=[]) as dispatch,
            ):
                dispatch_request = {"host": {"vmid": 1101}, "template": {}, "revision": "a" * 40}
                build_request.return_value = dispatch_request
                self.assertEqual(guests.main(), 0)
                forwarded = dispatch.call_args.args[0][0]
                self.assertEqual("kernel_source" in forwarded, operation == "--prepare-level")

    def test_native_bootstrap_executes_beyond_raw_argument_limit(self):
        argv = shlex.split(guests.host_ssh(request()["host"])[-1])
        self.assertLess(len(argv[-1].encode()), 131072)
        result = subprocess.run(argv, input="", text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error", json.loads(result.stdout))
        with patch.object(
            guests, "host_source", return_value="print('transport-ok')\n#" + "x" * 200000
        ):
            argv = shlex.split(guests.host_ssh(request()["host"])[-1])
            result = subprocess.run(argv, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "transport-ok\n")

    def test_exact_kernel_diagnostics_cross_both_boundaries(self):
        envelope = {
            "level_operation": "verify",
            "level": "kernel-src",
            "chain": [],
            "proposed": None,
        }
        message = "Kernel source HEAD differs; restore kernel-src or preserve existing tree"
        for error in (message, message + " private-secret"):
            stderr = io.StringIO()
            with (
                patch.object(g.sys, "stdin", io.StringIO(json.dumps(dict(envelope, request={})))),
                patch.object(g, "run_level", side_effect=g.GuestError(error)),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(g.main(), 1)
            self.assertNotIn("private-secret", stderr.getvalue())
            result = subprocess.CompletedProcess([], 1, "", stderr.getvalue())
            with (
                patch.object(guests, "guest_ssh", return_value=[]),
                patch.object(guests, "run_guest", return_value=result),
            ):
                with self.assertRaises(guests.ValidationError) as caught:
                    guests.guest_rpc({"host": {}}, Path("unused"), envelope, 60)
                self.assertEqual("HEAD differs" in str(caught.exception), error == message)


class KernelTreeTests(unittest.TestCase):
    def setUp(self):
        # Git hooks export an index path; fixture Git must not touch the caller's index.
        local_vars = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True)
        self.enterContext(patch.dict(os.environ))
        for name in local_vars.splitlines():
            os.environ.pop(name, None)
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.source = self.temp / "upstream"
        self.source.mkdir()
        self.git(self.source, "init", "--quiet")
        self.git(self.source, "config", "user.name", "Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / ".gitignore").write_text(".config\n*.o\n")
        (self.source / "Makefile").write_text("all:\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "--quiet", "-m", "fixture")
        self.commit = self.git(self.source, "rev-parse", "HEAD").strip()
        self.dest = self.temp / "home/src/linux"
        self.dest.parent.mkdir(parents=True)
        self.git(self.temp, "clone", "--quiet", "--depth=1", self.source.as_uri(), str(self.dest))
        self.git(self.dest, "checkout", "--quiet", "--detach", "HEAD")
        self.account = SimpleNamespace(
            pw_dir=str(self.temp / "home"), pw_uid=os.getuid(), pw_gid=os.getgid()
        )
        self.request = {"host": {"ansible_user": "operator"}}
        self.content = {"repo": REPO, "ref": "v6.9", "commit": self.commit}
        self.enterContext(patch.object(g.pwd, "getpwnam", return_value=self.account))
        self.enterContext(patch.object(g, "toolchain_source_path"))
        self.enterContext(
            patch.object(
                g,
                "operator_command",
                side_effect=lambda user, argv, timeout=60: g.command(argv, timeout),
            )
        )

    @staticmethod
    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
        )

    def test_clean_offline_tree_and_registry(self):
        g.check_kernel_source(self.request, self.content)
        self.assertEqual(g.level_chain("kernel-src")[-1]["parent"], "toolchain")
        self.request["kernel_source"] = {"repo": "bad", "ref": "bad"}
        g.check_kernel_source(self.request, self.content)
        for bad in (dict(self.content, commit="bad"), dict(self.content, extra=True)):
            with self.assertRaises(g.GuestError):
                g.check_kernel_source(self.request, bad)

    def test_dirty_untracked_and_ignored_fail_independently(self):
        for name in ("untracked", ".config", "output.o", "Makefile"):
            path = self.dest / name
            original = path.read_bytes() if path.exists() else None
            path.write_text("fault\n")
            with self.subTest(name=name), self.assertRaisesRegex(g.GuestError, "artifacts"):
                g.check_kernel_source(self.request, self.content)
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
            g.check_kernel_source(self.request, self.content)

    def test_wrong_head_branch_and_full_history_fail(self):
        with self.assertRaisesRegex(g.GuestError, "HEAD"):
            g.check_kernel_source(self.request, dict(self.content, commit="f" * 40))
        self.git(self.dest, "checkout", "-b", "branch")
        with self.assertRaisesRegex(g.GuestError, "detached"):
            g.check_kernel_source(self.request, self.content)
        self.git(self.dest, "checkout", "--detach")
        self.git(self.dest, "fetch", "--unshallow")
        with self.assertRaisesRegex(g.GuestError, "shallow"):
            g.check_kernel_source(self.request, self.content)

    def test_nested_wrong_owner_and_symlink_destination_fail(self):
        original = Path.lstat

        def metadata(path):
            info = original(path)
            if path == self.dest / "Makefile":
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        with patch.object(Path, "lstat", metadata):
            with self.assertRaisesRegex(g.GuestError, "owner"):
                g.check_kernel_source(self.request, self.content)
        real = self.dest.with_name("saved")
        self.dest.rename(real)
        self.dest.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(g.GuestError, "directory"):
            g.check_kernel_source(self.request, self.content)

    def test_fresh_prepare_fetches_exact_commit_depth_one(self):
        shutil.rmtree(self.dest)
        calls = []

        def run(user, argv, timeout=60):
            calls.append((argv, timeout))
            if "fetch" in argv:
                self.assertEqual(argv[-4:], ["--depth=1", "--", REPO, self.commit])
                self.assertEqual(timeout, 900)
                argv = [*argv[:3], "-c", "protocol.file.allow=always", *argv[3:]]
                argv[argv.index(REPO)] = self.source.as_uri()
            return g.command(argv, timeout)

        self.request["kernel_source"] = {"repo": REPO, "ref": self.commit}
        with patch.object(g, "operator_command", side_effect=run):
            content = g.prepare_kernel_source(self.request)
        self.assertEqual(content["commit"], self.commit)
        self.assertTrue(any("fetch" in argv for argv, _ in calls))
        g.check_kernel_source(self.request, content)

    def test_prepare_preserves_existing_mismatch(self):
        self.request["kernel_source"] = {"repo": REPO, "ref": "f" * 40}
        before = self.git(self.dest, "rev-parse", "HEAD")
        with self.assertRaisesRegex(g.GuestError, "HEAD"):
            g.prepare_kernel_source(self.request)
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), before)
        self.request["kernel_source"]["ref"] = self.commit
        self.assertEqual(g.prepare_kernel_source(self.request)["commit"], self.commit)


if __name__ == "__main__":
    unittest.main()
