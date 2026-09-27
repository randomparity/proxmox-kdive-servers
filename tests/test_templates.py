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


class NativeFixture:
    """Emulate the external native commands, retaining actual lifecycle state."""

    def __init__(self, request):
        self.request = request
        self.config = None
        self.pending = []
        self.status = "stopped"
        self.node = request["node"]
        self.storage = {
            "active": 1,
            "enabled": 1,
            "type": "zfspool",
            "content": "images",
            "avail": 100 * 1024**3,
        }
        self.bridge = {"iface": "vmbr0", "active": 1, "type": "bridge", "bridge_ports": "eno1"}
        self.volumes = []
        self.extent = 4 * 1024**2
        self.calls = []
        self.image_info = {
            "format": "qcow2",
            "virtual-size": request["image"]["virtual_size_bytes"],
        }

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "vgs":
            return json.dumps(
                {
                    "report": [
                        {"vg": [{"vg_name": "vg-example", "vg_extent_size": str(self.extent)}]}
                    ]
                }
            )
        if argv[0] == "qemu-img":
            return json.dumps(self.image_info)
        if argv[0] == "pvesh":
            path = argv[2]
            if path == "/storage/pool":
                return '{"type":"lvmthin","vgname":"vg-example"}'
            if path == "/cluster/status":
                return json.dumps([{"type": "node", "local": 1, "name": self.node}])
            if path == "/cluster/resources":
                return json.dumps(
                    []
                    if self.config is None
                    else [
                        {"vmid": self.request["template_vmid"], "node": self.node, "type": "qemu"}
                    ]
                )
            if path.endswith("/network"):
                return json.dumps([self.bridge])
            if path.endswith("/status/current"):
                return json.dumps({"status": self.status})
            if "/storage/" in path and path.endswith("/status"):
                return json.dumps(self.storage)
            if path.endswith("/content"):
                return json.dumps(self.volumes)
            if path.endswith("/config"):
                return json.dumps(self.config)
            if path.endswith("/pending"):
                return json.dumps(self.pending)
            if path.endswith("/feature"):
                return '{"hasFeature": 1}'
            raise AssertionError(path)
        if argv[:2] == ["qm", "create"]:
            opts = dict(zip(argv[3::2], argv[4::2], strict=True))
            self.config = {key.removeprefix("--"): value for key, value in opts.items()}
            self.config["net0"] = self.config["net0"].replace(
                "virtio,", "virtio=02:11:22:33:44:55,"
            )
            for slot in ["scsi0", "ide2", "efidisk0"]:
                suffix = {"scsi0": "disk-0", "ide2": "cloudinit", "efidisk0": "disk-1"}[slot]
                volid = f"pool:vm-{self.request['template_vmid']}-{suffix}"
                options = {
                    "scsi0": "size=4G",
                    "ide2": "media=cdrom",
                    "efidisk0": "efitype=4m,ms-cert=2023k,pre-enrolled-keys=1,size=4M",
                }[slot]
                self.config[slot] = f"{volid},{options}"
                self.volumes.append(
                    {
                        "volid": volid,
                        "vmid": self.request["template_vmid"],
                        "format": "raw",
                        "content": "images",
                        "size": self.request["image"]["virtual_size_bytes"]
                        if slot == "scsi0"
                        else 4 * 1024**2,
                    }
                )
                if self.storage["type"] == "lvmthin":
                    size = self.volumes[-1]["size"]
                    self.volumes[-1]["size"] = (size + self.extent - 1) // self.extent * self.extent
            return ""
        if argv[:2] == ["qm", "template"]:
            self.config["template"] = 1
            for slot in ["scsi0", "efidisk0"]:
                self.config[slot] = self.config[slot].replace(":vm-", ":base-")
            for volume in self.volumes:
                if "cloudinit" not in volume["volid"]:
                    volume["volid"] = volume["volid"].replace(":vm-", ":base-")
            return ""
        if argv[:2] == ["qm", "set"]:
            self.config["description"] = argv[-1]
            return ""
        raise AssertionError(argv)


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        import hashlib
        import tempfile
        from unittest.mock import patch

        from scripts import template_host

        self.host = template_host
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        image = json.loads((ROOT / "vars/images.json").read_text())["ubuntu"]
        self.payload = b"verified image fixture"
        image.update(size_bytes=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest())
        self.request = {
            "profile": "ubuntu",
            "template_vmid": 9000,
            "node": "node1",
            "storage": "pool",
            "bridge": "vmbr0",
            "vlan": 16,
            "cpu": "host",
            "image": image,
            "apply": True,
            "resume": False,
        }
        self.native = NativeFixture(self.request)
        for target, value in [
            ("command", self.native),
            ("CACHE", Path(self.temp.name) / "cache"),
            ("LOCKS", Path(self.temp.name) / "locks"),
        ]:
            context = patch.object(template_host, target, value)
            context.start()
            self.addCleanup(context.stop)
        for target, value in [
            ("platform.system", "Linux"),
            ("platform.machine", "x86_64"),
            ("os.geteuid", 0),
            ("shutil.which", "/bin/native"),
        ]:
            context = patch("scripts.template_host." + target, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        import io

        def download(*args, **kwargs):
            stream = io.BytesIO(self.payload)
            stream.geturl = lambda: "https://vendor.invalid/image"
            return stream

        context = patch("scripts.template_host.urllib.request.urlopen", side_effect=download)
        context.start()
        self.addCleanup(context.stop)

    def test_create_and_preserve(self):
        first = self.host.run(self.request)
        self.assertEqual(first["action"], "created")
        create = next(c for c in self.native.calls if c[:2] == ["qm", "create"])
        self.assertEqual(create[create.index("--cpu") + 1], "host")
        self.assertIn("virtio,bridge=vmbr0,tag=16", create)
        self.assertIn("ovmf", create)
        self.assertIn("nocloud", create)
        self.native.calls.clear()
        second = self.host.run(self.request)
        self.assertEqual(second["action"], "preserved")
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(first["config_sha256"], second["config_sha256"])
        self.assertFalse(any(c[0] == "qm" for c in self.native.calls))

    def test_native_efi_certificate_marker(self):
        self.host.run(self.request)
        self.assertIn("ms-cert=2023k", self.native.config["efidisk0"])
        self.native.config["efidisk0"] = self.native.config["efidisk0"].replace(
            "2023k", "unexpected"
        )
        with self.assertRaisesRegex(self.host.TemplateError, "certificate"):
            self.host.run(self.request)

    def test_lvmthin_extent_rounding_survives_lifecycle(self):
        size = json.loads((ROOT / "vars/images.json").read_text())["opensuse"]["virtual_size_bytes"]
        self.request["profile"] = "opensuse"
        self.request["image"]["virtual_size_bytes"] = size
        self.native.image_info["virtual-size"] = size
        self.native.storage["type"] = "lvmthin"
        try:
            result = self.host.run(self.request)
        except self.host.TemplateError as error:
            self.fail(f"Valid extent-rounded allocation was rejected: {error}")
        self.assertEqual(result["action"], "created")
        self.assertEqual(self.native.volumes[0]["size"], 1456 * 1024**2)
        self.assertEqual(self.host.run(self.request)["action"], "preserved")
        self.native.config["description"] = self.native.config["description"].replace(
            "ready", "creating"
        )
        self.request["resume"] = True
        self.assertEqual(self.host.run(self.request)["action"], "resumed")
        self.native.volumes[0]["size"] += self.native.extent
        with self.assertRaisesRegex(self.host.TemplateError, "allocation size"):
            self.host.run(self.request)

    def test_lvmthin_bad_extent_fails_before_allocation(self):
        self.native.storage["type"] = "lvmthin"
        for extent in (0, -1, "4.5", "unknown", 3000):
            with self.subTest(extent=extent):
                self.native.extent = extent
                with self.assertRaisesRegex(self.host.TemplateError, "extent"):
                    self.host.run(self.request)
                self.assertIsNone(self.native.config)
                self.assertFalse(any(c[0] == "qm" for c in self.native.calls))

    def test_lvmthin_invalid_geometry_response_fails_closed(self):
        from unittest.mock import patch

        self.native.storage["type"] = "lvmthin"
        for response in (
            "invalid JSON",
            "{}",
            '{"report":[]}',
            '{"report":[{"vg":[{"vg_name":"wrong","vg_extent_size":"4194304"}]}]}',
        ):
            with self.subTest(response=response):
                with patch.object(
                    self.host,
                    "command",
                    side_effect=lambda argv, response=response, **kwargs: (
                        response if argv[0] == "vgs" else self.native(argv, **kwargs)
                    ),
                ):
                    with self.assertRaisesRegex(self.host.TemplateError, "extent"):
                        self.host.run(self.request)
                self.assertIsNone(self.native.config)

    def test_lvmthin_admission_reserves_rounded_auxiliary_disks(self):
        self.native.storage["type"] = "lvmthin"
        self.native.extent = 16 * 1024**2
        self.native.storage["avail"] = self.request["image"]["virtual_size_bytes"] + 16 * 1024**2
        with self.assertRaisesRegex(self.host.TemplateError, "space"):
            self.host.run(self.request)
        self.assertIsNone(self.native.config)
        self.native.storage["avail"] += 16 * 1024**2
        self.assertEqual(self.host.run(self.request)["action"], "created")
        self.assertEqual(self.host.run(self.request)["action"], "preserved")

    def test_plan_never_writes_and_node_mismatch(self):
        self.request["apply"] = False
        self.assertEqual(self.host.run(self.request)["action"], "would-create")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.native.node = "different"
        with self.assertRaisesRegex(self.host.TemplateError, "node"):
            self.host.run(self.request)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_foreign_drift_and_storage(self):
        import copy

        for change in [{"active": 0}, {"enabled": 0}, {"type": "dir"}, {"avail": 1}]:
            original = self.native.storage.copy()
            self.native.storage.update(change)
            with self.assertRaises(self.host.TemplateError):
                self.host.run(self.request)
            self.assertIsNone(self.native.config)
            self.native.storage = original
        self.host.run(self.request)
        for field, value in [
            ("description", "foreign"),
            ("cpu", "x86-64-v2"),
            ("unused0", "pool:vm-9000-disk-8"),
            ("lock", "backup"),
        ]:
            original = copy.deepcopy(self.native.config)
            self.native.config[field] = value
            self.native.calls.clear()
            with self.assertRaises(self.host.TemplateError):
                self.host.run(self.request)
            self.assertFalse(any(c[0] == "qm" for c in self.native.calls))
            self.native.config = original
        self.native.volumes[0]["vmid"] = 1234
        with self.assertRaises(self.host.TemplateError):
            self.host.run(self.request)

    def test_partial_recovery(self):
        self.host.run(self.request)
        self.native.config["description"] = self.native.config["description"].replace(
            "ready", "creating"
        )
        with self.assertRaises(self.host.TemplateError):
            self.host.run(self.request)
        self.request["resume"] = True
        self.native.pending = [{"key": "memory", "pending": "4096"}]
        with self.assertRaises(self.host.TemplateError):
            self.host.run(self.request)
        self.native.pending = []
        self.assertEqual(self.host.run(self.request)["action"], "resumed")
        self.assertFalse(any("destroy" in c for c in self.native.calls))

    def test_resume_after_conversion_failure(self):
        from unittest.mock import patch

        def fail_conversion(argv, **kwargs):
            if argv[:2] == ["qm", "template"]:
                raise self.host.TemplateError("native conversion failed")
            return self.native(argv, **kwargs)

        with patch.object(self.host, "command", side_effect=fail_conversion):
            with self.assertRaises(self.host.TemplateError) as caught:
                self.host.run(self.request)
        self.assertEqual(caught.exception.phase, "creating")
        self.request["resume"] = True
        result = self.host.run(self.request)
        self.assertEqual(result["action"], "resumed")
        self.assertEqual(sum(c[:2] == ["qm", "create"] for c in self.native.calls), 1)

    def test_native_capability_and_disk_format_are_required(self):
        from unittest.mock import patch

        self.host.run(self.request)
        self.native.volumes[0]["format"] = "qcow2"
        with self.assertRaises(self.host.TemplateError):
            self.host.run(self.request)
        self.native.volumes[0]["format"] = "raw"

        def no_capability(argv, **kwargs):
            if argv[0] == "pvesh" and argv[2].endswith("/feature"):
                return '{"hasFeature": 0}'
            return self.native(argv, **kwargs)

        with patch.object(self.host, "command", side_effect=no_capability):
            with self.assertRaises(self.host.TemplateError):
                self.host.run(self.request)

    def test_download_integrity(self):
        self.payload = b"wrong length and digest"
        with self.assertRaises(self.host.TemplateError):
            self.host.run(self.request)
        self.assertIsNone(self.native.config)
        self.payload = b"verified image fixture"
        for change in [
            {"backing-filename": "parent"},
            {"encrypted": True},
            {"format": "raw"},
            {"virtual-size": 1},
            {"format-specific": {"data": {"data-file": "external"}}},
        ]:
            original = self.native.image_info.copy()
            self.native.image_info.update(change)
            with self.assertRaises(self.host.TemplateError):
                self.host.run(self.request)
            self.assertIsNone(self.native.config)
            self.native.image_info = original

    def test_download_deadline_interrupts_blocking_read(self):
        import io
        import signal
        import time
        from unittest.mock import patch

        class SlowResponse(io.BytesIO):
            def geturl(self):
                return "https://vendor.invalid/image"

            def read(self, size):
                # Model a socket read that keeps receiving data past the total deadline.
                time.sleep(0.2)
                return super().read(size)

        source = SlowResponse(self.payload)
        handler = signal.getsignal(signal.SIGALRM)
        with (
            patch.object(self.host, "DOWNLOAD_TIMEOUT", 0.03, create=True),
            patch("scripts.template_host.urllib.request.urlopen", return_value=source),
            self.assertRaisesRegex(self.host.TemplateError, "deadline"),
        ):
            self.host.run(self.request)
        self.assertIsNone(self.native.config)
        self.assertEqual(list(self.host.CACHE.iterdir()), [])
        self.assertTrue(source.closed)
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_incomplete_running_or_malformed_state_never_recovers(self):
        self.host.run(self.request)
        self.native.config["description"] = self.native.config["description"].replace(
            "ready", "creating"
        )
        self.request["resume"] = True
        self.native.status = "running"
        with self.assertRaises(self.host.TemplateError):
            self.host.run(self.request)
        self.native.status = "stopped"
        self.native.volumes.pop()
        self.native.calls.clear()
        with self.assertRaises(self.host.TemplateError) as caught:
            self.host.run(self.request)
        self.assertEqual(caught.exception.phase, "creating")
        self.assertFalse(any(c[0] == "qm" for c in self.native.calls))

    def test_existing_cache_corruption_and_symlink_are_rejected(self):
        self.host.run(self.request)
        cache = next(self.host.CACHE.glob("*.qcow2"))
        cache.write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(self.host.TemplateError, "SHA256"):
            self.host.cached_image(self.request["image"])
        cache.unlink()
        target = Path(self.temp.name) / "other"
        target.write_bytes(self.payload)
        cache.symlink_to(target)
        with self.assertRaises(self.host.TemplateError):
            self.host.cached_image(self.request["image"])

    def test_native_error_never_means_absence(self):
        from unittest.mock import patch

        with patch.object(
            self.host, "command", side_effect=self.host.TemplateError("native failed")
        ):
            with self.assertRaises(self.host.TemplateError):
                self.host.run(self.request)
        self.assertIsNone(self.native.config)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_lock_contention(self):
        import subprocess
        import sys

        self.host.private_directory(self.host.LOCKS)
        lock = self.host.LOCKS / "9000.lock"
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,sys,os; f=open(sys.argv[1],'w'); os.chmod(sys.argv[1],0o600); "
                "fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); "
                "sys.stdin.read()",
                str(lock),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            self.assertEqual(process.stdout.readline().strip(), "held")
            try:
                with self.assertRaises(self.host.TemplateError):
                    self.host.run(self.request)
                self.assertIsNone(self.native.config)
            finally:
                process.communicate("")
        self.assertEqual(self.host.run(self.request)["action"], "created")


class TestController(unittest.TestCase):
    def setUp(self):
        from scripts import templates
        from scripts.validate_inventory import load_inventory

        self.controller = templates
        self.host = load_inventory(ROOT / "inventory/example.yml")["_meta"]["hostvars"][
            "ubuntu_local"
        ]
        self.host.update(
            proxmox_ssh_host="pve.invalid", proxmox_ssh_user="root", cpu="host", template_vmid=9000
        )

    def test_api_deadline_interrupts_blocking_response(self):
        import io
        import os
        import signal
        import time
        from unittest.mock import patch

        from scripts.validate_inventory import ValidationError

        class SlowResponse(io.BytesIO):
            def read(self, size):
                time.sleep(0.2)
                return super().read(size)

        source = SlowResponse(b'{"data":{"cpuinfo":{"cpus":8},"memory":{"total":1024}}}')
        storage = io.BytesIO(
            b'{"data":{"active":1,"enabled":1,"type":"zfspool","content":"images"}}'
        )
        self.addCleanup(storage.close)
        handler = signal.getsignal(signal.SIGALRM)
        env = {self.host[key]: "credential" for key in self.controller.CREDENTIAL_REFS}
        with (
            patch.dict(os.environ, env),
            patch.object(self.controller, "API_TIMEOUT", 0.03, create=True),
            patch("urllib.request.OpenerDirector.open", side_effect=[source, storage]),
            self.assertRaises(ValidationError),
        ):
            self.controller.api_admission(self.host)
        self.assertTrue(source.closed)
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_api_ssh_failures_are_fatal(self):
        import os
        import subprocess
        from unittest.mock import patch

        from scripts.validate_inventory import ValidationError

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError):
                self.controller.api_admission(self.host)
        env = {
            self.host[key]: "SECRET_SENTINEL"
            for key in ("api_user_env", "api_token_id_env", "api_token_secret_env")
        }
        with (
            patch.dict(os.environ, env),
            patch("urllib.request.OpenerDirector.open", side_effect=OSError("SECRET_SENTINEL")),
        ):
            with self.assertRaises(ValidationError) as caught:
                self.controller.api_admission(self.host)
            self.assertNotIn("SECRET_SENTINEL", str(caught.exception))
        request = self.controller.request_for(self.host, False, False)
        for result in [
            subprocess.CompletedProcess([], 255, "", "SECRET_SENTINEL"),
            subprocess.CompletedProcess([], 0, "{}", ""),
            subprocess.CompletedProcess([], 0, "SECRET_SENTINEL", ""),
        ]:
            with patch("scripts.templates.subprocess.run", return_value=result):
                with self.assertRaises(ValidationError) as caught:
                    self.controller.dispatch(self.host, request)
                self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_positive_api_evidence_required(self):
        import io
        import os
        from unittest.mock import patch

        from scripts.validate_inventory import ValidationError

        env = {
            self.host[key]: "credential"
            for key in ("api_user_env", "api_token_id_env", "api_token_secret_env")
        }
        for data in [{}, [], {"data": []}, {"data": {}}, {"data": {"uptime": 1}}]:
            with (
                patch.dict(os.environ, env),
                patch(
                    "urllib.request.OpenerDirector.open",
                    side_effect=lambda *a, data=data, **kw: io.BytesIO(json.dumps(data).encode()),
                ),
            ):
                with self.assertRaises(ValidationError):
                    self.controller.api_admission(self.host)

    def test_dispatch_binds_result_and_hides_credentials(self):
        import subprocess
        from unittest.mock import patch

        from scripts import template_host

        request = self.controller.request_for(self.host, False, False)
        result = {
            "profile": "ubuntu",
            "template_vmid": 9000,
            "identity": template_host.identity(request),
            "action": "would-create",
            "config_sha256": "a" * 64,
            "duration_seconds": 1.0,
        }
        with patch(
            "scripts.templates.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(result), ""),
        ) as run:
            self.assertEqual(self.controller.dispatch(self.host, request), result)
            arguments = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", arguments)
            self.assertIn("BatchMode=yes", arguments)
            self.assertNotIn("api_user_env", run.call_args.kwargs["input"])


class TestEntryPoints(unittest.TestCase):
    def test_cli_and_playbook(self):
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder)
            (fixture / "sitecustomize.py").write_text("""
import io,json,subprocess,urllib.request
from scripts import template_host
original = subprocess.run

def open_api(self, request, **kwargs):
    data = {"active":1,"enabled":1,"type":"zfspool","content":"images"}
    if "/storage/" not in request.full_url:
        data = {"cpuinfo":{"cpus":8},"memory":{"total":1024**3}}
    return io.BytesIO(json.dumps({"data":data}).encode())

def run(argv, **kwargs):
    if argv[0] != "ssh":
        return original(argv, **kwargs)
    request = json.loads(kwargs["input"])
    result = {"profile":request["profile"], "template_vmid":request["template_vmid"],
              "identity":template_host.identity(request), "config_sha256":"a"*64,
              "duration_seconds":1.0, "action":"created" if request["apply"] else "would-create"}
    return subprocess.CompletedProcess(argv,0,json.dumps(result),"")
urllib.request.OpenerDirector.open = open_api
subprocess.run = run
""")
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in {"INVENTORY", "TARGETS", "APPLY", "RESUME"}
            }
            env.update(
                PYTHONPATH=str(fixture) + os.pathsep + str(ROOT),
                PROXMOX_API_USER="user",
                PROXMOX_API_TOKEN_ID="token",
                PROXMOX_API_TOKEN_SECRET="SECRET_SENTINEL",
            )
            python = str(ROOT / ".venv/bin/python")
            cli = [python, "scripts/templates.py"]
            result = subprocess.run(
                cli, env=env, cwd=ROOT, text=True, capture_output=True, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            for apply in [False, True]:
                selected = dict(env, TARGETS="ubuntu_local")
                if apply:
                    selected["APPLY"] = "1"
                result = subprocess.run(
                    cli, env=selected, cwd=ROOT, text=True, capture_output=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["action"], "created" if apply else "would-create"
                )
                playbook = [
                    str(ROOT / ".venv/bin/ansible-playbook"),
                    "-i",
                    "localhost,",
                    "playbooks/templates.yml",
                ]
                result = subprocess.run(
                    playbook,
                    env=selected,
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("changed=1" if apply else "changed=0", result.stdout)
                self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
