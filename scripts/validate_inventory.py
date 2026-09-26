"""Validate trusted static Ansible inventory without contacting the lab."""

import argparse
import base64
import binascii
import ipaddress
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {"ubuntu", "fedora", "rocky", "opensuse"}
CREDENTIAL_REFS = ("api_user_env", "api_token_id_env", "api_token_secret_env")
PLAINTEXT_CREDENTIALS = {
    "api_user",
    "api_password",
    "api_token_id",
    "api_token_secret",
    "proxmox_api_user",
    "proxmox_api_password",
    "proxmox_api_token_id",
    "proxmox_api_token_secret",
    "ansible_password",
    "ansible_ssh_pass",
    "ansible_become_password",
    "ansible_become_pass",
    "ansible_ssh_password",
    "ansible_sudo_pass",
    "ansible_su_pass",
    "ansible_private_key",
    "ansible_ssh_private_key",
    "ansible_private_key_passphrase",
    "ansible_ssh_private_key_passphrase",
}


class ValidationError(ValueError):
    """An actionable error whose text contains no supplied inventory values."""


def require(condition, field, rule):
    if not condition:
        raise ValidationError(f"{field}: {rule}")


def text_field(value, field, pattern):
    require(
        isinstance(value, str) and re.fullmatch(pattern, value) is not None,
        field,
        "supply a literal value in the documented format",
    )


def integer(value, field, minimum=1, maximum=2147483647):
    require(
        type(value) is int and minimum <= value <= maximum,
        field,
        f"supply an integer from {minimum} to {maximum}, not a Boolean",
    )


def ipv4(value, field):
    require(isinstance(value, str), field, "supply an IPv4 address")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValidationError(f"{field}: supply an IPv4 address") from None
    require(
        not (
            address == ipaddress.IPv4Address("255.255.255.255")
            or address.is_unspecified
            or address.is_multicast
            or address.is_loopback
            or address.is_link_local
        ),
        field,
        "supply a usable unicast IPv4 address",
    )
    return address


def validate_key(value):
    require(
        isinstance(value, str) and "\n" not in value and "\r" not in value,
        "ssh_public_keys",
        "supply one OpenSSH public key per list item",
    )
    parts = value.split()
    require(len(parts) >= 2, "ssh_public_keys", "supply an OpenSSH algorithm and key blob")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        fields = []
        while blob:
            length = struct.unpack(">I", blob[:4])[0]
            require(0 < length <= len(blob) - 4, "ssh_public_keys", "invalid key blob")
            fields.append(blob[4 : 4 + length])
            blob = blob[4 + length :]
        require(
            fields and fields[0] == parts[0].encode("ascii"),
            "ssh_public_keys",
            "algorithm must match the key blob",
        )
    except (ValueError, binascii.Error, struct.error, UnicodeError):
        raise ValidationError("ssh_public_keys: supply a valid base64 OpenSSH key blob") from None
    algorithm = parts[0]
    valid = algorithm == "ssh-ed25519" and len(fields) == 2 and len(fields[1]) == 32
    if algorithm == "ssh-rsa":
        valid = len(fields) == 3 and all(int.from_bytes(item) > 0 for item in fields[1:])
    if algorithm in {"ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
        sizes = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
        curve = algorithm.removeprefix("ecdsa-sha2-")
        valid = (
            len(fields) == 3
            and fields[1] == curve.encode("ascii")
            and len(fields[2]) == sizes[curve]
            and fields[2][0] == 4
        )
    require(valid, "ssh_public_keys", "supply an Ed25519, RSA or NIST ECDSA public key")


def validate_host(host):
    require(isinstance(host, dict), "hostvars", "supply host variables for each managed target")
    require(
        not PLAINTEXT_CREDENTIALS.intersection(host),
        "credentials",
        "remove plaintext credential variables and use the documented environment references",
    )
    require(
        isinstance(host.get("profile"), str) and host["profile"] in PROFILES,
        "profile",
        "choose ubuntu, fedora, rocky or opensuse",
    )
    integer(host.get("vmid"), "vmid", 100, 999999999)
    integer(host.get("proxmox_api_port", 8006), "proxmox_api_port", 1, 65535)
    for field in ("cores", "memory_mib", "disk_gib"):
        integer(host.get(field), field)
    if "vlan" in host:
        integer(host["vlan"], "vlan", 1, 4094)
    text_field(
        host.get("proxmox_api_host"),
        "proxmox_api_host",
        r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
    )
    for field in ("proxmox_node", "storage", "bridge", "ansible_user"):
        text_field(host.get(field), field, r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
    for field in CREDENTIAL_REFS:
        text_field(host.get(field), field, r"[A-Z_][A-Z0-9_]{0,127}")
    require(
        len({host[field] for field in CREDENTIAL_REFS}) == 3,
        "credentials",
        "use distinct user, token ID and token secret environment names",
    )
    address = ipv4(host.get("ansible_host"), "ansible_host")
    cidr = host.get("ipv4_cidr")
    require(isinstance(cidr, str) and "/" in cidr, "ipv4_cidr", "supply IPv4 address/prefix")
    try:
        interface = ipaddress.IPv4Interface(cidr)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        raise ValidationError("ipv4_cidr: supply a valid IPv4 address/prefix") from None
    require(address == interface.ip, "ipv4_cidr", "address must equal ansible_host")
    gateway = ipv4(host.get("gateway"), "gateway")
    network = interface.network
    require(
        gateway in network and gateway != address,
        "gateway",
        "supply a different address in the target subnet",
    )
    if network.prefixlen < 31:
        endpoints = {network.network_address, network.broadcast_address}
        require(
            address not in endpoints, "ansible_host", "use a host address, not subnet/broadcast"
        )
        require(gateway not in endpoints, "gateway", "use a host address, not subnet/broadcast")
    dns = host.get("dns_servers")
    require(isinstance(dns, list) and dns, "dns_servers", "supply a non-empty IPv4 address list")
    for value in dns:
        ipv4(value, "dns_servers")
    keys = host.get("ssh_public_keys")
    require(
        isinstance(keys, list) and keys, "ssh_public_keys", "supply a non-empty public key list"
    )
    for value in keys:
        validate_key(value)


def managed_hosts(data):
    require(
        isinstance(data, dict) and isinstance(data.get("kdive"), dict),
        "inventory",
        "define a non-empty kdive group in static YAML",
    )
    names, seen, pending = set(), set(), ["kdive"]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        group = data.get(name)
        require(isinstance(group, dict), "inventory", "define each managed child group")
        for field in ("hosts", "children"):
            values = group.get(field, [])
            require(
                isinstance(values, list) and all(isinstance(value, str) for value in values),
                "inventory",
                "group hosts and children must contain names",
            )
        names.update(group.get("hosts", []))
        pending.extend(group.get("children", []))
    require(names, "inventory", "define at least one managed target in the kdive group")
    hostvars = data.get("_meta", {}).get("hostvars", {})
    require(
        isinstance(hostvars, dict) and names <= hostvars.keys(),
        "inventory",
        "supply variables for each managed target",
    )
    return {name: hostvars[name] for name in sorted(names)}


def validate_inventory(data, targets=None):
    hosts = managed_hosts(data)
    ids, addresses = set(), set()
    for host in hosts.values():
        validate_host(host)
        require(host["vmid"] not in ids, "vmid", "IDs must be unique across managed targets")
        require(
            host["ansible_host"] not in addresses,
            "ansible_host",
            "addresses must be unique across managed targets",
        )
        ids.add(host["vmid"])
        addresses.add(host["ansible_host"])
    if targets is None:
        return len(hosts)
    require(isinstance(targets, str), "targets", "supply comma-separated exact inventory aliases")
    selected = targets.split(",")
    require(
        all(name in hosts for name in selected) and len(set(selected)) == len(selected),
        "targets",
        "supply distinct existing aliases, without patterns or empty entries",
    )
    return len(selected)


def load_inventory(path):
    path = Path(path)
    try:
        valid_file = path.suffix in {".yml", ".yaml"} and path.is_file()
    except OSError:
        raise ValidationError(
            "inventory: cannot inspect source file; check path and permissions"
        ) from None
    require(
        valid_file,
        "inventory",
        "supply an existing static .yml or .yaml inventory file",
    )
    env = dict(
        os.environ,
        ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"),
        ANSIBLE_INVENTORY_ENABLED="yaml",
        ANSIBLE_DUPLICATE_YAML_DICT_KEY="error",
        ANSIBLE_INVENTORY_UNPARSED_IS_FAILED="True",
        ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED="True",
    )
    try:
        result = subprocess.run(
            [str(Path(sys.executable).with_name("ansible-inventory")), "-i", str(path), "--list"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValidationError(
            "inventory: loader unavailable or timed out; run make setup and retry"
        ) from None
    require(
        result.returncode == 0 and not result.stderr.strip(),
        "inventory",
        "Ansible could not parse static YAML cleanly; check syntax, duplicates and group structure",
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValidationError("inventory: loader returned invalid JSON; run make setup") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", default=os.environ.get("INVENTORY", ROOT / "inventory/example.yml")
    )
    parser.add_argument("--targets", default=os.environ.get("TARGETS"))
    args = parser.parse_args()
    try:
        count = validate_inventory(load_inventory(args.inventory), args.targets)
    except ValidationError as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Inventory valid: {count} selected target(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
