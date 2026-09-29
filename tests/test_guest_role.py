"""Verify the guest role surfaces only capacity warnings from hidden controller output."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB = """import sys
print('{"action": "created", "secret": "stdout-secret"}')
print("Guest capacity warning: node pve1 requests 4 cores", file=sys.stderr)
print("private-detail: stderr-secret", file=sys.stderr)
print("Guest capacity warning: node pve1 requests 8192 MiB", file=sys.stderr)
"""
PLAYBOOK = """---
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    proxmox_guest_verify_only: false
  roles:
    - role: proxmox_guest
"""


class TestGuestRoleWarnings(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        (self.work / "scripts").mkdir()
        (self.work / "playbooks").mkdir()
        (self.work / "scripts" / "guests.py").write_text(STUB)
        (self.work / "playbooks" / "run.yml").write_text(PLAYBOOK)

    def run_playbook(self):
        env = {**os.environ, "ANSIBLE_ROLES_PATH": str(ROOT / "roles")}
        ansible = Path(sys.executable).parent / "ansible-playbook"
        return subprocess.run(
            [str(ansible), "-i", "localhost,", str(self.work / "playbooks" / "run.yml")],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_warnings_shown_and_other_output_hidden(self):
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Guest capacity warning: node pve1 requests 4 cores", result.stdout)
        self.assertIn("Guest capacity warning: node pve1 requests 8192 MiB", result.stdout)
        self.assertNotIn("stderr-secret", result.stdout)
        self.assertNotIn("stdout-secret", result.stdout)


if __name__ == "__main__":
    unittest.main()
