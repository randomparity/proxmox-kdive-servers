"""Exercise controller deadlines using local children, never managed hosts."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import guest_host, guests, templates
from scripts.validate_inventory import ValidationError
from tests.test_guests import request

ROOT = Path(__file__).resolve().parents[1]


class TestOperationDeadline(unittest.TestCase):
    def setUp(self):
        self.handler = signal.getsignal(signal.SIGALRM)
        self.timer = signal.getitimer(signal.ITIMER_REAL)
        self.addCleanup(signal.signal, signal.SIGALRM, self.handler)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, *self.timer)

    def test_required_duration_and_mode_reject_before_admission(self):
        for mode, value in [
            ("--verify", None),
            ("--restore", "20"),
            ("--verify", "86401"),
            ("--verify", "2.5"),
            ("--verify", "True"),
            ("--teardown", "21"),
        ]:
            argv = ["guests.py", mode, "--targets", "ubuntu"]
            if value is not None:
                argv += ["--operation-timeout-seconds", value]
            with (
                self.subTest(mode=mode, value=value),
                patch.object(sys, "argv", argv),
                patch.object(guests, "load_inventory") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(guests.main(), 1)
                load.assert_not_called()

    def test_remaining_budget_does_not_reset_and_refuses_new_children(self):
        deadline = guests.OperationDeadline("21")
        self.assertLessEqual(deadline.remaining(1800), 1)
        deadline.work_end = time.monotonic() - 1
        with patch.object(guests.subprocess, "Popen") as spawn:
            with self.assertRaises(guests.OperationExpired):
                deadline.spawn(["UNREACHABLE"])
        spawn.assert_not_called()

    def test_existing_timer_handler_and_blocked_mask_are_refused(self):
        signal.setitimer(signal.ITIMER_REAL, 10)
        with self.assertRaises(ValidationError):
            guests.OperationDeadline("21")
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, lambda *_: None)
        with self.assertRaises(ValidationError):
            guests.OperationDeadline("21")
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            with self.assertRaises(ValidationError):
                guests.OperationDeadline("21")
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)

    def test_api_success_keeps_outer_timer_and_legacy_call_still_works(self):
        host = request()["host"]
        responses = [
            {"cpuinfo": {"cpus": 8}, "memory": {"total": 1024}},
            {"active": 1, "enabled": 1, "type": "zfspool", "content": "images"},
        ]
        env = {host[k]: "fixture" for k in templates.CREDENTIAL_REFS}
        deadline = guests.OperationDeadline("21")
        with (
            patch.dict(os.environ, env),
            patch.object(templates.ssl, "create_default_context"),
            patch(
                "urllib.request.OpenerDirector.open",
                side_effect=[io.BytesIO(json.dumps({"data": r}).encode()) for r in responses],
            ),
        ):
            templates.api_admission(host, operation_deadline=deadline)
        self.assertIs(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 19)
        signal.setitimer(signal.ITIMER_REAL, 0)
        with (
            patch.dict(os.environ, env),
            patch.object(templates.ssl, "create_default_context"),
            patch(
                "urllib.request.OpenerDirector.open",
                side_effect=[io.BytesIO(json.dumps({"data": r}).encode()) for r in responses],
            ),
        ):
            templates.api_admission(host)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_make_forwards_timeout_as_data_in_one_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
            stub = root / ".venv/bin/python"
            stub.parent.mkdir(parents=True)
            stub.write_text(
                f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                "Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
            )
            stub.chmod(0o700)
            value = '21" $(printf unexpected) --apply'
            result = subprocess.run(
                ["make", "verify"],
                cwd=root,
                capture_output=True,
                text=True,
                env={**os.environ, "OPERATION_TIMEOUT_SECONDS": value},
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((root / "argv.json").read_text()),
                ["scripts/guests.py", "--verify", "--operation-timeout-seconds", value],
            )

    def test_bounded_dispatch_completes_actual_plan_protocol(self):
        req = request()
        event = dict(
            vmid=req["host"]["vmid"],
            identity=guest_host.identity(req),
            phase="planned",
            action="would-restore",
        )
        source = (
            "import sys,json; json.loads(sys.stdin.readline()); print("
            + repr(json.dumps(event))
            + ", flush=True)"
        )
        deadline = guests.OperationDeadline("60")
        try:
            with patch.object(guests, "host_ssh", return_value=[sys.executable, "-c", source]):
                result = guests.dispatch(
                    [req], "plan-restore", Path("/unused"), operation_deadline=deadline
                )
            self.assertEqual(result[0]["action"], "would-restore")
        finally:
            deadline.cleanup()

    def test_registered_guest_transport_preserves_success_and_output_cap(self):
        deadline = guests.OperationDeadline("60")
        try:
            result = guests.run_guest(
                [sys.executable, "-c", "print('valid')"], timeout=10, operation_deadline=deadline
            )
            self.assertEqual((result.returncode, result.stdout), (0, "valid\n"))
            with self.assertRaisesRegex(ValidationError, "response exceeds"):
                guests.run_guest(
                    [sys.executable, "-c", "print('x' * 131072)"],
                    timeout=10,
                    operation_deadline=deadline,
                )
        finally:
            deadline.cleanup()

    def test_cleanup_reaps_owned_children_after_pipe_close_failure(self):
        deadline = guests.OperationDeadline("21")
        process = deadline.spawn(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait, timeout=2)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        with patch.object(process, "stdin") as broken:
            broken.close.side_effect = OSError("fixture")
            with self.assertRaisesRegex(ValidationError, "local cleanup"):
                deadline.cleanup()
        self.assertIsNotNone(process.poll())


class TestRealDeadlineProcesses(unittest.TestCase):
    def child(self, source, timeout=5):
        return subprocess.run(
            [sys.executable, "-c", source],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def test_native_alarm_ends_c_only_work_without_python_callback(self):
        result = self.child("""
import hashlib, time
from scripts.guests import OperationDeadline
d = OperationDeadline('21')
d.arm(time.monotonic() + .15)
hashlib.pbkdf2_hmac('sha256', b'x', b'y', 100000000)
""")
        self.assertEqual(result.returncode, -signal.SIGALRM, result.stderr)

    def test_api_real_wrapper_stall_obeys_phase_and_cannot_clear_outer_alarm(self):
        result = self.child("""
import os, time
from unittest.mock import patch
from scripts import guests, templates
from tests.test_guests import request
host = request()['host']
os.environ.update({host[k]: 'fixture' for k in templates.CREDENTIAL_REFS})
d = guests.OperationDeadline('21')
d.work_end = time.monotonic() + .15
with patch.object(templates.ssl, 'create_default_context'), patch(
    'urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: time.sleep(10)):
    templates.api_admission(host, operation_deadline=d)
""")
        self.assertEqual(result.returncode, -signal.SIGALRM, result.stderr)

    def test_guest_input_stall_expires_and_cleanup_reaps_local_child(self):
        result = self.child("""
import subprocess, sys, time
from scripts import guests
d = guests.OperationDeadline('21')
d.work_end = time.monotonic() + .15
try:
    guests.run_guest([sys.executable, '-c', 'import time; time.sleep(10)'],
                     input='x' * 262144, timeout=1800, operation_deadline=d)
except (guests.OperationExpired, subprocess.TimeoutExpired):
    d.cleanup()
    assert all(p.poll() is not None for p in d.children)
    sys.exit(0)
raise AssertionError('blocked request did not expire')
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_event_and_live_child_after_eof_obey_work_cutoff(self):
        for source in [
            "import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(10)",
            "import os,time; os.close(1); os.close(2); time.sleep(10)",
        ]:
            with self.subTest(source=source):
                result = self.child(f"""
import subprocess, sys, time
from scripts import guests
d = guests.OperationDeadline('21')
d.work_end = time.monotonic() + .15
try:
    p = d.spawn([sys.executable, '-c', {source!r}], stdout=subprocess.PIPE,
                stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    guests.read_event(p, operation_deadline=d)
except (guests.OperationExpired, guests.ValidationError):
    d.cleanup()
    assert p.poll() is not None
    sys.exit(0)
raise AssertionError('partial or missing event admitted')
""")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_dispatch_request_stall_is_bounded(self):
        result = self.child("""
import sys, time
from pathlib import Path
from unittest.mock import patch
from scripts import guests
from tests.test_guests import request
d = guests.OperationDeadline('21')
d.work_end = time.monotonic() + .15
r = request(); r['padding'] = 'x' * 262144
try:
    with patch.object(guests, 'host_ssh', return_value=[
        sys.executable, '-c', 'import time; time.sleep(10)']):
        guests.dispatch([r], 'plan-restore', Path('/unused'), operation_deadline=d)
except guests.OperationExpired:
    d.cleanup()
    assert all(p.poll() is not None for p in d.children)
    sys.exit(0)
raise AssertionError('blocked dispatch request admitted')
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_cli_shares_deadline_across_admission_plan_and_groups(self):
        result = self.child("""
import os, sys
from unittest.mock import patch
from scripts import guests
from tests.test_guests import request
base_request = request()
data = guests.load_inventory(guests.ROOT / 'inventory/example.yml')
for alias in ('fedora', 'fedora-install'):
    data['_meta']['hostvars'][alias]['proxmox_node'] = 'other-node'
owners = []
def api(*args, operation_deadline, **kwargs):
    owners.append(operation_deadline)
def dispatch(*args, operation_deadline, **kwargs):
    assert all(x is operation_deadline for x in owners)
    os.write(1, (args[1] + '\\n').encode())
    return []
sys.argv = ['guests.py', '--restore', '--apply', '--confirm', 'ubuntu,fedora',
            '--exclusive', '--targets', 'ubuntu,fedora', '--operation-timeout-seconds', '60']
with patch.object(guests, 'load_inventory', return_value=data), patch.object(
    guests.templates, 'api_admission', side_effect=api), patch.object(
    guests, 'request_for', side_effect=lambda host, *a: dict(base_request, host=host)
), patch.object(
    guests, 'dispatch', side_effect=dispatch):
    guests.main()
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["plan-restore"] * 2 + ["restore"] * 2)

    def test_output_stall_cannot_turn_deadline_into_success(self):
        result = self.child("""
import os, time
from scripts import guests
d = guests.OperationDeadline('21')
d.work_end = time.monotonic() + .15
read, write = os.pipe()
try:
    with os.fdopen(write, 'wb', buffering=0) as stream:
        try:
            d.write(stream, b'x' * 262144)
        except guests.OperationExpired:
            os._exit(124)
finally:
    os.close(read)
raise AssertionError('blocked output succeeded')
""")
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_deadline_cleanup_skips_implicit_python_finalizers(self):
        result = self.child("""
import atexit, sys, time
from unittest.mock import patch
from scripts import guests
atexit.register(lambda: time.sleep(10))
sys.argv = ['guests.py', '--verify', '--targets', 'ubuntu', '--operation-timeout-seconds', '21']
with patch.object(guests, 'load_inventory', side_effect=guests.ValidationError('fixture')):
    guests.main()
""")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("remote state unverified", result.stderr)
        self.assertIn("Guest operation failed: fixture", result.stderr)

    def test_local_cleanup_budget_is_shared_across_term_ignoring_children(self):
        result = self.child(
            """
import subprocess, sys, time
from scripts import guests
d = guests.OperationDeadline('21')
source = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); " \
         "print('{}', flush=True); time.sleep(10)"
try:
    for _ in range(2):
        p = d.spawn([sys.executable, '-c', source], stdout=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        guests.read_event(p, operation_deadline=d)
    started = time.monotonic()
    d.cleanup()
    assert time.monotonic() - started < 6.5
    assert all(p.poll() is not None for p in d.children)
finally:
    for p in d.children:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=2)
""",
            timeout=9,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_interrupt_and_termination_during_cli_work(self):
        for name, code in [("SIGINT", 130), ("SIGTERM", -signal.SIGTERM)]:
            with self.subTest(signal=name):
                result = self.child(f"""
import signal, sys
from unittest.mock import patch
from scripts import guests
sys.argv = ['guests.py', '--verify', '--targets', 'ubuntu', '--operation-timeout-seconds', '21']
with patch.object(guests, 'load_inventory',
                  side_effect=lambda *a: signal.raise_signal(signal.{name})):
    guests.main()
""")
                self.assertEqual(result.returncode, code, result.stderr)

    def test_interrupt_and_unexpected_error_cannot_exit_success(self):
        for exception, code in [("KeyboardInterrupt()", 130), ("RuntimeError('fixture')", 1)]:
            with self.subTest(exception=exception):
                result = self.child(f"""
import sys
from unittest.mock import patch
from scripts import guests
sys.argv = ['guests.py', '--verify', '--targets', 'ubuntu', '--operation-timeout-seconds', '21']
with patch.object(guests, 'load_inventory', side_effect={exception}):
    guests.main()
""")
                self.assertEqual(result.returncode, code, result.stderr)


if __name__ == "__main__":
    unittest.main()
