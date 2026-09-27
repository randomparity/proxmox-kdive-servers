"""Plan or import explicitly selected, verified Proxmox templates."""

import argparse
import json
import math
import os
import re
import shlex
import signal
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_TIMEOUT = 60

if __package__:
    from . import template_host
    from .validate_inventory import (
        CREDENTIAL_REFS,
        ROOT,
        ValidationError,
        load_inventory,
        managed_hosts,
        require,
        validate_inventory,
    )
else:
    import template_host
    from validate_inventory import (
        CREDENTIAL_REFS,
        ROOT,
        ValidationError,
        load_inventory,
        managed_hosts,
        require,
        validate_inventory,
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValidationError("API redirect refused; configure the direct verified endpoint")


def api_admission(host, *, source=False):
    def deadline_expired(signum, frame):
        raise ValidationError("API admission exceeded its total deadline")

    values = [os.environ.get(host[field], "") for field in CREDENTIAL_REFS]
    require(
        all(value and not any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values),
        "credentials",
        "set the referenced API credential environment variables",
    )
    user, token, secret = values
    previous_handler = signal.signal(signal.SIGALRM, deadline_expired)
    try:
        # Bound the complete admission even when a response keeps making slow progress.
        signal.setitimer(signal.ITIMER_REAL, API_TIMEOUT)
        context = ssl.create_default_context(cafile=host.get("proxmox_api_ca_file") or None)
        opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPSHandler(context=context)
        )
        base = f"https://{host['proxmox_api_host']}:{host.get('proxmox_api_port', 8006)}/api2/json"
        responses = []
        paths = [
            f"/nodes/{host['proxmox_node']}/status",
            f"/nodes/{host['proxmox_node']}/storage/{host['storage']}/status",
        ]
        if source:
            paths.append(f"/nodes/{host['proxmox_node']}/qemu/{host['template_vmid']}/config")
        for path in paths:
            request = urllib.request.Request(
                base + path, headers={"Authorization": f"PVEAPIToken={user}!{token}={secret}"}
            )
            with opener.open(request, timeout=30) as response:
                raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024, "API", "response exceeded size bound")
            result = json.loads(raw)
            require(
                isinstance(result, dict) and isinstance(result.get("data"), dict),
                "API",
                "missing positive node/storage evidence; check token audit permissions",
            )
            responses.append(result["data"])
    except (OSError, urllib.error.URLError, ValueError):
        raise ValidationError(
            "API admission failed; check CA, endpoint and token audit permissions"
        ) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    node, storage = responses[:2]
    cpu = node.get("cpuinfo", {})
    memory = node.get("memory", {})
    require(
        isinstance(cpu, dict)
        and type(cpu.get("cpus")) is int
        and cpu["cpus"] > 0
        and isinstance(memory, dict)
        and type(memory.get("total")) is int
        and memory["total"] > 0,
        "API",
        "missing positive node evidence; check token audit permissions",
    )
    require(
        storage.get("active") == 1
        and storage.get("enabled") == 1
        and storage.get("type") in {"zfspool", "lvmthin"}
        and "images" in str(storage.get("content", "")).split(","),
        "API",
        "selected storage must be active image-capable zfspool or lvmthin",
    )
    return responses[2] if source else None


def request_for(host, apply, resume):
    profiles = json.loads((ROOT / "vars/images.json").read_text())
    return {
        "profile": host["profile"],
        "template_vmid": host["template_vmid"],
        "node": host["proxmox_node"],
        "storage": host["storage"],
        "bridge": host["bridge"],
        "vlan": host.get("vlan"),
        "cpu": host["cpu"],
        "image": profiles[host["profile"]],
        "apply": apply,
        "resume": resume,
    }


def dispatch(host, request):
    source = (ROOT / "scripts/template_host.py").read_text()
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(host.get("proxmox_ssh_port", 22)),
        "-l",
        host["proxmox_ssh_user"],
    ]
    if host.get("proxmox_ssh_private_key_file"):
        argv.extend(
            [
                "-i",
                str(Path(host["proxmox_ssh_private_key_file"]).expanduser()),
                "-o",
                "IdentitiesOnly=yes",
            ]
        )
    argv.extend(["--", host["proxmox_ssh_host"], "python3 -c " + shlex.quote(source)])
    try:
        process = subprocess.run(
            argv,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValidationError(
            "SSH unavailable or timed out; inspect host task state before retry"
        ) from None
    if process.returncode != 0:
        phase = "unknown"
        try:
            failure = json.loads(process.stdout)
            if isinstance(failure, dict) and failure.get("error") == "template-operation-failed":
                candidate = failure.get("phase")
                if candidate in {"absent", "creating", "ready", "unowned", "unknown"}:
                    phase = candidate
        except (ValueError, TypeError):
            pass
        raise ValidationError(
            f"SSH: template operation failed; observed phase {phase}; "
            "inspect private host state before retry"
        )
    require(not process.stderr.strip(), "SSH", "unexpected diagnostics; inspect host state")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        raise ValidationError("SSH: invalid template result; inspect host state") from None
    keys = {"profile", "template_vmid", "identity", "action", "config_sha256", "duration_seconds"}
    require(
        isinstance(result, dict)
        and set(result) == keys
        and result["profile"] == request["profile"]
        and type(result["template_vmid"]) is int
        and result["template_vmid"] == request["template_vmid"]
        and result["identity"] == template_host.identity(request),
        "SSH",
        "template result identity mismatch; inspect host state",
    )
    allowed = (
        {"preserved", "created", "resumed"} if request["apply"] else {"preserved", "would-create"}
    )
    require(
        result["action"] in allowed
        and isinstance(result["config_sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", result["config_sha256"])
        and type(result["duration_seconds"]) in {int, float}
        and math.isfinite(result["duration_seconds"])
        and result["duration_seconds"] >= 0,
        "SSH",
        "invalid template outcome; inspect host state",
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", default=os.environ.get("INVENTORY", ROOT / "inventory/example.yml")
    )
    parser.add_argument("--targets", default=os.environ.get("TARGETS"))
    parser.add_argument("--apply", action="store_true", default=os.environ.get("APPLY") == "1")
    parser.add_argument("--resume", action="store_true", default=os.environ.get("RESUME") == "1")
    args = parser.parse_args()
    try:
        require(args.targets is not None, "targets", "select explicit comma-separated aliases")
        require(not args.resume or args.apply, "resume", "requires --apply")
        data = load_inventory(args.inventory)
        validate_inventory(data, args.targets, purpose="templates")
        hosts = managed_hosts(data)
        seen = set()
        for alias in args.targets.split(","):
            host = hosts[alias]
            if host["template_vmid"] in seen:
                continue
            seen.add(host["template_vmid"])
            api_admission(host)
            result = dispatch(host, request_for(host, args.apply, args.resume))
            print(json.dumps(result, sort_keys=True), flush=True)
    except ValidationError as error:
        print(f"Template operation failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("Template operation failed: invalid local configuration or result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
