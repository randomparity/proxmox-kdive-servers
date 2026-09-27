# Controller and inventory bootstrap

Approved by the operator for #2 on 2026-09-26; scope token `q2-17aec9ff`.
See [ADR 0001](../../adr/0001-offline-inventory-validation.md).

## Problem and scope

A fresh checkout needs isolated tools and a private, ordinary Ansible inventory
before #3–#5 can add live operations. There is no existing executable owner or
caller to migrate. This change owns only controller setup and offline validation.
Python 3.12+ runs on macOS/Linux controllers; managed guests are x86_64.
No image/release/template contract, provisioning, capacity measurement, snapshots,
KDIVE installation, or lab access is introduced. Owners remain #3, #4, #5,
the KDIVE workflow, and the operator as frozen in WORK:SCOPE.

### Failure model

- Actors/deployments: a repository contributor or trusted lab operator running
  local checks on macOS/Linux, and GitHub-hosted CI using the anonymous example.
- Invariants/assets: one Ansible inventory, explicit target identity, unique VM
  IDs, actionable value-free errors, no live operations or credential resolution.
- Accepted: inventory is trusted local configuration, not a sandbox for adversarial
  Ansible plugins or arbitrary inherited controller environment; offline success
  cannot prove a node, storage, bridge, credential, key ownership or capacity exists.
  Huge machine limits are syntactically bounded, not advertised as usable capacity.
- Other owners: #3 image/template identity and storage admission; #4 real capacity,
  connection/ownership, guest readiness and KVM; #5 destructive lifecycle safety;
  operator private inputs; KDIVE installation/tests and external state.

## Inventory and selection

Use static `.yml`/`.yaml` ordinary Ansible inventory and a non-empty `kdive` group.
The built-in YAML plugin resolves nested groups and inherited variables; dynamic
inventory plugins are disabled for validation. Duplicate YAML keys, parser errors,
empty inventory and a missing/empty managed group fail closed.
Each managed host supplies the following, directly or through ordinary group vars:

| Fields | Offline rules |
| --- | --- |
| `profile` | `ubuntu`, `fedora`, `rocky`, or `opensuse`; family only, no release promise |
| `vmid` | integer 100–999999999, not Boolean; unique across the entire managed group |
| `proxmox_api_host`, `proxmox_node`, `storage`, `bridge` | non-empty host/identifier strings without whitespace or templates |
| `proxmox_api_port` | integer 1–65535; default 8006 |
| `api_user_env`, `api_token_id_env`, `api_token_secret_env` | distinct uppercase environment-variable names, never their values |
| `ansible_host`, `ipv4_cidr`, `gateway`, `dns_servers` | usable static IPv4, CIDR agrees with host, gateway in subnet and distinct, non-empty IPv4 DNS list |
| `ansible_user`, `ssh_public_keys` | non-empty account identifier and list of SSH public key lines with supported algorithm and valid base64 key blob |
| `cores`, `memory_mib`, `disk_gib` | positive integers up to 2147483647, not Boolean |
| `vlan` | optional integer 1–4094, not Boolean |

The validator checks the complete managed inventory before reporting selection.
Repeated IP addresses fail. Exact comma-separated `TARGETS` aliases select hosts;
omission selects all for this read-only check. Empty, duplicate, unknown aliases
or pattern expressions fail. Several hosts may share one profile. No VM ID derives
from its IP address. Plaintext API or Ansible password/token variables are rejected;
unknown non-contract Ansible variables are preserved and not evaluated by this check.
The source is private configuration: never print its raw JSON, YAML or parser errors.

## Components and commands

`uv` 0.12.19 creates `.venv` from a committed lock with exact ansible-core 2.21.4,
ansible-lint 26.9.0, Ruff 0.16.9 and pre-commit 4.6.2. Python's stdlib supplies the
validator and unittest runner. No Proxmox client or collection is needed yet.
`ansible.cfg` keeps SSH host-key checking enabled and makes duplicate/unparsed YAML
fatal. `scripts/validate_inventory.py` invokes the sibling `ansible-inventory`
executable with captured output, no stdin and a bounded timeout, then validates the
resolved JSON. Diagnostics report field/rule without supplied values.
`playbooks/validate.yml` runs only on the controller with facts disabled; its sole
validation command calls that same script using the INVENTORY environment input.
Invoke it with `-i localhost,`, a fixed controller-only bootstrap inventory, so
Ansible never preloads the private source outside the captured validator boundary.
It has no network or mutating module, and suppresses raw command arguments/output.
`make validate INVENTORY=... TARGETS=...` uses the script; direct playbook execution
is a supported equivalent. `make syntax` syntax-checks the validation playbook.
`make lint`, `make syntax`, `make validate`, and `make test` form `make check`.
`make setup` performs locked dependency installation; `make hooks` installs the
pre-commit hook, which invokes `make check` with the anonymous default inventory.
CI installs the same tools and invokes `make check` on Linux and macOS, without
lab inputs. Actions use immutable commit pins, read-only permissions, no secrets.

## Success and validation

A fresh isolated checkout installs the locked environment and passes `make check`.
Unit and CLI tests reject missing/wrong types, invalid profiles, global ID/address
collisions, invalid selections, malformed static networking, credential-reference
mistakes, malformed public keys, resource boundaries and parse failures. A secret
sentinel in malformed input and in an environment variable never reaches diagnostics.
Tests exercise actual Ansible YAML loading/inheritance and the controller playbook,
including malformed private input with a secret sentinel and private source path.
A controlled mutation of an invariant demonstrates the regression suite fails.
The installed hook and CI execute the real shared recipe; timing and tested commits
are recorded in the PR, with live infrastructure proof explicitly owned by #3–#5.
README describes tools, commands, fields, four-family matrix, private-file handling,
and the operator's prerequisite inventory: node/version, permissions, storage/bridge,
measured resources, concurrency, exclusive VM assignments and SSH access before #4.
