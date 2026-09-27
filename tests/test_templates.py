"""Verify image provenance and safe template lifecycle boundaries."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestProfiles(unittest.TestCase):
    def test_four_pins(self):
        profiles = json.loads((ROOT / "vars/images.json").read_text())
        self.assertEqual(set(profiles), {"ubuntu", "fedora", "rocky", "opensuse"})
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                self.assertTrue(profile["url"].startswith("https://"))
                self.assertNotIn("latest", profile["url"])
                self.assertRegex(profile["sha256"], r"^[a-f0-9]{64}$")
                self.assertGreater(profile["size_bytes"], 1)
                self.assertGreater(profile["virtual_size_bytes"], profile["size_bytes"])
                self.assertEqual(profile["bios"], "ovmf")
                baseline = profile["baseline"]
                self.assertEqual(baseline["architecture"], "x86_64")
                self.assertEqual(baseline["customization"], "none")
                self.assertTrue(baseline["nocloud"])
                self.assertRegex(baseline["packages_sha256"], r"^[a-f0-9]{64}$")
                self.assertGreater(baseline["package_count"], 0)
                self.assertIn("cloud-init", baseline["management_packages"])
