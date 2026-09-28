"""Exercise the inventory contract and its real controller entry points."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validate_inventory import ValidationError, ipv4, load_inventory, validate_inventory

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "inventory/example.yml"
PYTHON = str(ROOT / ".venv/bin/python")
VALIDATOR = str(ROOT / "scripts/validate_inventory.py")


class TestValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = load_inventory(EXAMPLE)

    def setUp(self):
        self.data = copy.deepcopy(self.example)
        self.host = self.data["_meta"]["hostvars"]["ubuntu"]

    def test_valid_and_independent_selection(self):
        self.assertEqual(validate_inventory(self.data), 4)
        self.assertEqual(validate_inventory(self.data, "ubuntu,fedora"), 2)
        self.host["profile"] = "fedora"
        self.assertEqual(validate_inventory(self.data), 4)

    def test_shared_template_allows_independent_guest_vlan_only(self):
        other = self.data["_meta"]["hostvars"]["fedora"]
        other.update(
            profile=self.host["profile"], template_vmid=self.host["template_vmid"], vlan=25
        )
        self.assertEqual(validate_inventory(self.data), 4)
        self.assertEqual(validate_inventory(self.data, purpose="templates"), 4)
        other["proxmox_node"] = "node2"
        with self.assertRaises(ValidationError):
            validate_inventory(self.data)

    def test_guest_hardware_options_and_shared_source(self):
        other = self.data["_meta"]["hostvars"]["fedora"]
        other.update(
            profile=self.host["profile"],
            template_vmid=self.host["template_vmid"],
            cpu="x86-64-v3",
            bridge="vmbr2",
            storage="other-pool",
            vlan=25,
            nic_queues=4,
            balloon_mib=4096,
        )
        for purpose in ("guests", "templates"):
            self.assertEqual(validate_inventory(self.data, purpose=purpose), 4)
        for field, values in {
            "cpu": [None, True, "bad model", "host,flags=oops"],
            "nic_model": ["unknown", None, True],
            "nic_queues": [-1, 65, True, "4"],
            "balloon_mib": [-1, self.host["memory_mib"] + 1, True, "4096"],
        }.items():
            for value in values:
                candidate = copy.deepcopy(self.data)
                candidate["_meta"]["hostvars"]["ubuntu"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    validate_inventory(candidate)

    def test_invalid_field_types_and_values(self):
        cases = {
            "profile": [None, "unknown", True, "{{ lookup('env', 'SECRET') }}"],
            "vmid": [True, 99, 1000000000, "1101", None],
            "cores": [False, 0, -1, 2147483648, 1.5],
            "memory_mib": [False, 0, -1, "4096", 2147483648],
            "disk_gib": [False, 0, -1, [], 2147483648],
            "vlan": [False, 0, 4095, "2"],
            "proxmox_api_port": [False, 0, 65536, "8006"],
            "proxmox_api_host": ["", "https://pve.example.invalid", "bad host", None],
            "proxmox_node": ["", "bad node", ";bad", None],
            "storage": ["", "bad name", None],
            "bridge": ["", "bad name", None],
            "ansible_user": ["", "bad user", None],
            "api_user_env": ["", "bad-name", "{{ secret }}", None],
            "api_token_id_env": ["", "bad-name", "PROXMOX_API_USER"],
            "api_token_secret_env": ["", "bad-name", "PROXMOX_API_USER"],
            "ansible_host": ["", "::1", "127.0.0.1", "224.0.0.1", "192.0.2.0", 1],
            "ipv4_cidr": ["", "192.0.2.12/24", "192.0.2.11", "192.0.2.11/33", None],
            "gateway": ["", "192.0.3.1", "192.0.2.11", "192.0.2.255", None],
            "dns_servers": [
                [],
                "192.0.2.53",
                ["invalid"],
                [True],
                ["0.0.0.0"],
                ["255.255.255.255"],
            ],
            "ssh_public_keys": [[], "ssh-ed25519 AAAA", ["invalid"], ["ssh-ed25519 AAAA"]],
        }
        for field, values in cases.items():
            original = self.host.get(field)
            for value in values:
                with self.subTest(field=field, value=value):
                    self.host[field] = value
                    with self.assertRaises(ValidationError):
                        validate_inventory(self.data)
            if original is None:
                self.host.pop(field, None)
            else:
                self.host[field] = original

    def test_required_fields_and_plaintext_credentials(self):
        for field in list(self.host):
            if field in {
                "proxmox_api_port",
                "vlan",
                "cpu",
                "template_vmid",
                "proxmox_ssh_host",
                "proxmox_ssh_user",
                "proxmox_ssh_port",
            }:
                continue
            with self.subTest(missing=field):
                value = self.host.pop(field)
                with self.assertRaises(ValidationError):
                    validate_inventory(self.data)
                self.host[field] = value
        for field in [
            "api_token_secret",
            "api_password",
            "ansible_password",
            "ansible_become_pass",
            "ansible_ssh_password",
            "ansible_sudo_pass",
            "ansible_su_pass",
            "ansible_private_key",
            "ansible_ssh_private_key",
            "ansible_private_key_passphrase",
            "ansible_ssh_private_key_passphrase",
        ]:
            with self.subTest(secret=field):
                self.host[field] = "SECRET_SENTINEL"
                with self.assertRaises(ValidationError) as error:
                    validate_inventory(self.data)
                self.assertNotIn("SECRET_SENTINEL", str(error.exception))
                del self.host[field]

    def test_global_collisions_even_when_unselected(self):
        other = self.data["_meta"]["hostvars"]["rocky"]
        for field in ["vmid", "ansible_host"]:
            with self.subTest(field=field):
                original = other[field]
                other[field] = self.host[field]
                cidr = other["ipv4_cidr"]
                if field == "ansible_host":
                    other["ipv4_cidr"] = self.host["ipv4_cidr"]
                with self.assertRaisesRegex(ValidationError, "unique"):
                    validate_inventory(self.data, "fedora")
                other[field] = original
                other["ipv4_cidr"] = cidr

    def test_selection_rejects_empty_patterns_duplicates_and_unknowns(self):
        for selection in ["", "all", "*", "missing", "ubuntu,", "ubuntu,ubuntu"]:
            with self.subTest(selection=selection), self.assertRaises(ValidationError):
                validate_inventory(self.data, selection)

    def test_special_networks_are_rejected(self):
        for field in ["ansible_host", "gateway", "dns_servers"]:
            for value in ["0.0.0.11", "240.0.0.11"]:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    ipv4(value, field)

    def test_integer_boundaries_and_small_subnet(self):
        for field in ["cores", "memory_mib", "disk_gib"]:
            self.host[field] = 2147483647
        self.host["vlan"] = 4094
        self.host["vmid"] = 100
        self.host["proxmox_api_port"] = 65535
        self.host["ipv4_cidr"] = "192.0.2.11/31"
        self.host["gateway"] = "192.0.2.10"
        self.assertEqual(validate_inventory(self.data), 4)

    def test_nested_groups_and_nonmanaged_hosts(self):
        hosts = self.data["kdive"].pop("hosts")
        self.data["kdive"]["children"] = ["test_guests"]
        self.data["test_guests"] = {"hosts": hosts}
        self.data["_meta"]["hostvars"]["unrelated"] = {"vmid": self.host["vmid"]}
        self.assertEqual(validate_inventory(self.data), 4)
        self.data["test_guests"]["hosts"] = []
        with self.assertRaises(ValidationError):
            validate_inventory(self.data)

    def test_key_blob_algorithm_and_truncation(self):
        key = self.host["ssh_public_keys"][0]
        for broken in [key.replace("ssh-ed25519", "ssh-rsa", 1), " ".join(key.split()[:2])[:-4]]:
            self.host["ssh_public_keys"] = [broken]
            with self.assertRaises(ValidationError):
                validate_inventory(self.data)

    def test_guest_dns_identity(self):
        original = self.host.get("fqdn")
        for value in [
            None,
            "short",
            "UPPER.example.invalid",
            "a..invalid",
            "-a.example",
            "a.example.",
            "a_b.example",
            "a" * 64 + ".example",
            "1.2.3.4",
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.host["fqdn"] = value
                validate_inventory(self.data)
        self.host["fqdn"] = original
        self.data["_meta"]["hostvars"]["rocky"]["fqdn"] = original
        with self.assertRaisesRegex(ValidationError, "unique"):
            validate_inventory(self.data, "fedora")

    def test_guest_validation_checks_template_collisions(self):
        self.host["vmid"] = self.data["_meta"]["hostvars"]["rocky"]["template_vmid"]
        with self.assertRaisesRegex(ValidationError, "collide"):
            validate_inventory(self.data, "fedora")


class TestTemplateValidation(unittest.TestCase):
    def setUp(self):
        self.data = load_inventory(EXAMPLE)
        for index, host in enumerate(self.data["_meta"]["hostvars"].values()):
            host.update(
                template_vmid=9000 + index,
                cpu="host",
                proxmox_ssh_host="pve.invalid",
                proxmox_ssh_user="root",
                proxmox_ssh_port=22,
            )
        self.host = self.data["_meta"]["hostvars"]["ubuntu"]

    def test_guest_inputs_can_wait(self):
        for host in self.data["_meta"]["hostvars"].values():
            host.update(
                vmid=None,
                ansible_host=None,
                ipv4_cidr=None,
                gateway=None,
                dns_servers=[],
                proxmox_ssh_private_key_file=None,
            )
        self.assertEqual(validate_inventory(self.data, purpose="templates"), 4)
        with self.assertRaises(ValidationError):
            validate_inventory(self.data)

    def test_template_identity_collisions(self):
        other = self.data["_meta"]["hostvars"]["rocky"]
        other["template_vmid"] = self.host["template_vmid"]
        with self.assertRaisesRegex(ValidationError, "template_vmid"):
            validate_inventory(self.data, "fedora", purpose="templates")
        other["profile"] = self.host["profile"]
        self.assertEqual(validate_inventory(self.data, purpose="templates"), 4)
        for field, value in [("proxmox_node", "node2"), ("proxmox_api_host", "different.invalid")]:
            with self.subTest(field=field):
                original = other.copy()
                other[field] = value
                with self.assertRaises(ValidationError):
                    validate_inventory(self.data, purpose="templates")
                other.clear()
                other.update(original)
        other["vmid"] = self.host["template_vmid"]
        with self.assertRaises(ValidationError):
            validate_inventory(self.data, purpose="templates")

    def test_template_literal_inputs(self):
        for field, values in {
            "cpu": [None, "bad model", "{{ value }}"],
            "template_vmid": [None, True, 99, 1000000000],
            "vlan": [False, 0, 4095, "12"],
            "proxmox_ssh_host": ["-option", "bad host", "$(command)"],
            "proxmox_ssh_user": [None, "bad user", "-option"],
            "proxmox_ssh_port": [True, 0, 65536],
            "proxmox_ssh_private_key_file": ["x\nSECRET_SENTINEL", "{{ key }}", 1],
            "proxmox_api_ca_file": ["{{ ca }}", "x\x00SECRET_SENTINEL", 1],
            "api_user_env": [None, "{{ secret }}"],
            "api_token_secret": ["SECRET_SENTINEL"],
        }.items():
            original = self.host.copy()
            for value in values:
                with self.subTest(field=field):
                    with self.assertRaises(ValidationError) as caught:
                        self.host[field] = value
                        validate_inventory(self.data, purpose="templates")
                    self.assertNotIn("SECRET_SENTINEL", str(caught.exception))
            self.host.clear()
            self.host.update(original)


class TestCLI(unittest.TestCase):
    def run_cli(self, *args, env=None):
        clean_env = {k: v for k, v in os.environ.items() if k not in {"INVENTORY", "TARGETS"}}
        clean_env.update(env or {})
        return subprocess.run(
            [PYTHON, VALIDATOR, *args],
            cwd=ROOT,
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    def test_example_and_inheritance(self):
        result = self.run_cli("--targets", "ubuntu")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Inventory valid: 1 selected target(s).\n")
        self.assertEqual(result.stderr, "")
        host = load_inventory(EXAMPLE)["_meta"]["hostvars"]["ubuntu"]
        self.assertEqual(host["memory_mib"], 32768)

    def test_bad_sources_are_safe(self):
        sources = [
            "",
            "all: [SECRET_SENTINEL",
            "all:\n  SECRET_SENTINEL: bad\n",
            "all:\n  vars:\n    a: 1\n    a: 2\n",
            "all:\n  vars:\n    a: 1\n",
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private-source.yml"
            for source in sources:
                with self.subTest(source=source):
                    path.write_text(source)
                    result = self.run_cli("--inventory", str(path))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
                    self.assertNotIn(str(path), result.stdout + result.stderr)
            result = self.run_cli("--inventory", str(Path(folder) / "missing.yml"))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(folder, result.stderr)

    def test_path_stat_failure_is_safe(self):
        result = self.run_cli("--inventory", "PRIVATE_SENTINEL" * 25 + ".yml")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PRIVATE_SENTINEL", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_environment_references_are_not_resolved(self):
        result = self.run_cli(env={"PROXMOX_API_TOKEN_SECRET": "SECRET_SENTINEL"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
        result = self.run_cli(env={"TARGETS": ""})
        self.assertNotEqual(result.returncode, 0)

    def test_template_contract_value_is_rejected(self):
        data = load_inventory(EXAMPLE)["_meta"]["hostvars"]
        data["ubuntu"]["api_user_env"] = "{{ lookup('env', 'PROXMOX_API_USER') }}"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "literal.yml"
            path.write_text(json.dumps({"all": {"children": {"kdive": {"hosts": data}}}}))
            result = self.run_cli(
                "--inventory", str(path), env={"PROXMOX_API_USER": "SECRET_SENTINEL"}
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)

    def test_loader_timeout_and_missing_tool_are_safe(self):
        for failure in [
            subprocess.TimeoutExpired("SECRET_SENTINEL", 30),
            OSError("SECRET_SENTINEL"),
        ]:
            with self.subTest(failure=type(failure).__name__):
                with patch("scripts.validate_inventory.subprocess.run", side_effect=failure):
                    with self.assertRaises(ValidationError) as error:
                        load_inventory(EXAMPLE)
                self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_playbook(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / "PRIVATE_SENTINEL.yml"
            private.write_text("all: [SECRET_SENTINEL")
            for source, expected in [(EXAMPLE, 0), (private, 2)]:
                env = dict(os.environ, INVENTORY=str(source))
                env.pop("TARGETS", None)
                result = subprocess.run(
                    [
                        str(ROOT / ".venv/bin/ansible-playbook"),
                        "-i",
                        "localhost,",
                        "playbooks/validate.yml",
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
                self.assertNotIn("PRIVATE_SENTINEL", result.stdout + result.stderr)
                if expected == 0:
                    self.assertIn("changed=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
