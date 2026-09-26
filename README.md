# Proxmox KDIVE server infrastructure

This repository prepares clean Linux VMs for
[KDIVE validation](https://github.com/randomparity/kdive/issues/2803).
Currently it provides the controller environment and **offline inventory checks**.
It does not yet create VMs, import images, restore snapshots, or install KDIVE.

## Controller setup

Use a macOS or Linux controller with Git, Make and
[uv 0.12.19](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.12 or newer is required by the pinned Ansible release; `.python-version`
selects Python 3.12 and uv installs it if needed. The controller may be ARM64 or
x86_64. Managed guest targets are **x86_64**, independently of the controller.

```sh
make setup
make check
make hooks
```

`make setup` creates `.venv` using `uv.lock`, including exact versions of
Ansible Core (2.21.4), ansible-lint (26.9.0), Ruff (0.16.9), and pre-commit (4.6.2).
Versions were checked against the package registry on 2026-09-26. Setup needs
network access to download tools; subsequent checks need no lab access or credentials.
`make hooks` installs the repository's local pre-commit hook. The hook and both
Linux/macOS CI jobs call the same `make check` recipe. The hook uses the anonymous
example even when your shell selects a private inventory.

Individual checks are `make lint`, `make syntax`, `make validate`, and `make test`.
Run Python commands through `.venv/bin/python` to use the installed controller.
Update dependency pins intentionally, regenerate `uv.lock` with `uv lock`, then
run the shared checks. The lock includes transitive dependencies and hashes.

## Private inventory

The tracked [example](inventory/example.yml) is ordinary static Ansible YAML,
with anonymous documentation addresses and a public key that grants no access.
It demonstrates four independently selectable families, not verified releases:

| Alias | Family | Architecture |
| --- | --- | --- |
| `ubuntu_local` | Ubuntu | x86_64 |
| `fedora_remote` | Fedora | x86_64 |
| `rocky_local` | Rocky Linux | x86_64 |
| `opensuse_remote` | openSUSE | x86_64 |

Copy it into the ignored private inventory directory and replace its placeholders:

```sh
mkdir -p inventory/private
cp inventory/example.yml inventory/private/lab.yml
chmod 700 inventory/private
chmod 600 inventory/private/lab.yml
make validate INVENTORY=inventory/private/lab.yml
make validate INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local,fedora_remote
```

Only `inventory/example.yml` is admitted by the inventory ignore rules. Keep real
inventory, credentials, SSH material, vault passwords, `.env` files and reports
untracked. Never use `git add -f` for these inputs. Git ignore rules prevent accidental
staging in these locations; they are not a secret scanner or file-permission manager.
Private command output from tools outside this validator also stays private.

Managed hosts belong to `kdive`, directly or through child groups. Ansible group
variables provide defaults; per-host variables override them. Add several aliases
with the same profile for separate local/remote lanes or resource variants.
Other groups may coexist, but these checks only validate managed `kdive` hosts.

| Input | Required value |
| --- | --- |
| `profile` | `ubuntu`, `fedora`, `rocky`, or `opensuse` |
| `vmid` | Explicit integer 100–999999999, unique throughout `kdive` |
| `proxmox_api_host`, `proxmox_api_port` | Hostname/IP without URL scheme; port 1–65535, default 8006 |
| `proxmox_node`, `storage`, `bridge` | Literal identifiers; existence is checked later against the lab |
| `api_user_env`, `api_token_id_env`, `api_token_secret_env` | Distinct uppercase environment-variable names, not credentials |
| `ansible_host`, `ipv4_cidr` | Matching static IPv4 host address and address/prefix |
| `gateway`, `dns_servers` | Different usable gateway in the subnet; non-empty IPv4 DNS list |
| `ansible_user`, `ssh_public_keys` | Guest account and non-empty OpenSSH public-key list (Ed25519/RSA/NIST ECDSA) |
| `cores`, `memory_mib`, `disk_gib` | Positive integer sizing, at most 2147483647; no Boolean/string coercion |
| `vlan` | Optional integer 1–4094 |

IDs and guest IPv4 addresses must be globally unique within `kdive`, even when a
subset is selected. The complete managed inventory is checked before selection.
Omitted `TARGETS` validates all for this **read-only** operation. Explicit empty,
unknown or duplicate aliases and Ansible patterns such as `all` or `*` fail.
Use literal values for these fields; templated contract values are rejected.
Static IPv4 is the initial contract. The `/31` point-to-point form is accepted;
normal subnet/broadcast addresses, unusable addresses and mismatched gateways fail.
The broad sizing bound prevents malformed inputs; it is not a Proxmox capacity claim.

Credential names are checked without reading their environment values. There is no
need to set them for offline checks. Recognizable plaintext API credentials and
Ansible password variables are rejected; unrelated Ansible variables are preserved.
This does not sanitize arbitrary private data or sandbox trusted inventory/plugins.
Public-key checks cover encoding/structure, not key ownership or successful login.
Do not put private keys in `ssh_public_keys`.

The same validator is available as a controller-only playbook:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local \
  .venv/bin/ansible-playbook -i localhost, playbooks/validate.yml
```

Keep the fixed `-i localhost,` bootstrap: passing private inventory to Ansible's
`-i` parses it before task output protection. Only the validator should load the
private source. It captures parser diagnostics and reports field/rule errors without
input values. The playbook hides task output; run `make validate` for safe details.

## Before live operations

The operator supplies a private record of Proxmox version/node, API permissions,
bridge/VLAN/storage, snapshot support, measured free CPU/RAM/disk, intended concurrency,
SSH access, and exclusive VM assignments. Offline success verifies none of these.
[Issue #4](https://github.com/randomparity/proxmox-kdive-servers/issues/4) must verify
these prerequisites before mutation; unavailable capacity/access is a failed prerequisite.

[Issue #3](https://github.com/randomparity/proxmox-kdive-servers/issues/3) owns exact
releases/images/checksums, template identity and storage admission. Issue #4 owns
cloning, sizing, authenticated readiness and actual nested KVM.
[Issue #5](https://github.com/randomparity/proxmox-kdive-servers/issues/5) owns clean
snapshots, restore, serialization and scoped teardown. These extend this inventory
and validation entry point instead of adding a second configuration system.

KDIVE owns installation, libvirt/build/debug tools, runners, guest qualification,
and test results/external state. Provisioning stops at documented management
prerequisites (SSH, sudo, Python, cloud-init and guest agent), preserving security
enforcement. A clean VM or snapshot does not prove KDIVE support for a distro.
