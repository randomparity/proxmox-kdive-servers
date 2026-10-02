# Private inventory

The tracked [example](../inventory/example.yml) is ordinary static Ansible YAML,
with anonymous documentation addresses and a public key that grants no access.
It demonstrates four independently selectable profiles; exact image pins and inspected
baselines are recorded in [vars/images.json](../vars/images.json):

| Alias | Family | Architecture |
| --- | --- | --- |
| `ubuntu` | Ubuntu | x86_64 |
| `fedora` | Fedora | x86_64 |
| `rocky` | Rocky Linux | x86_64 |
| `opensuse` | openSUSE | x86_64 |

Copy it into the ignored private inventory directory and replace its placeholders:

```sh
mkdir -p inventory/private
cp inventory/example.yml inventory/private/lab.yml
chmod 700 inventory/private
chmod 600 inventory/private/lab.yml
make validate INVENTORY=inventory/private/lab.yml
make validate INVENTORY=inventory/private/lab.yml TARGETS=ubuntu,fedora
```

Only `inventory/example.yml` is admitted by the inventory ignore rules. Keep real
inventory, credentials, SSH material, vault passwords, `.env` files and reports
untracked. Never use `git add -f` for these inputs. Git ignore rules prevent accidental
staging in these locations; they are not a secret scanner or file-permission manager.
Private command output from tools outside this validator also stays private.

Managed hosts belong to `kdive`, directly or through child groups. Ansible group
variables provide defaults; per-host variables override them. Add several aliases
with the same profile when you need multiple instances or resource variants.
Other groups may coexist, but these checks only validate managed `kdive` hosts.

| Input | Required value |
| --- | --- |
| `profile` | `ubuntu`, `fedora`, `rocky`, or `opensuse` |
| `vmid` | Explicit integer 100–999999999, unique throughout `kdive` |
| `fqdn` | Unique lowercase fully qualified guest DNS name, separate from SSH IPv4 |
| `proxmox_api_host`, `proxmox_api_port` | Hostname/IP without URL scheme; port 1–65535, default 8006 |
| `proxmox_node`, `storage`, `bridge` | Literal identifiers; existence is checked later against the lab |
| `api_user_env`, `api_token_id_env`, `api_token_secret_env` | Distinct uppercase environment-variable names, not credentials |
| `ansible_host`, `ipv4_cidr` | Matching static IPv4 host address and address/prefix |
| `gateway`, `dns_servers` | Different usable gateway in the subnet; non-empty IPv4 DNS list |
| `ansible_user`, `ssh_public_keys` | Guest account and non-empty OpenSSH public-key list (Ed25519/RSA/NIST ECDSA) |
| `cores`, `memory_mib`, `disk_gib` | Positive integer sizing, at most 2147483647; no Boolean/string coercion |
| `vlan` | Optional integer 1–4094; guest NIC tag; omit for an untagged guest |
| `nic_model`, `nic_queues` | NIC model (default `virtio`); optional integer 0–64 queues, only for virtio |
| `balloon_mib` | Balloon target in MiB, default `memory_mib`; 0 disables, otherwise at most `memory_mib` |
| `cloudinit_snippet_storage` | Required for `ubuntu` guests only, rejected for other profiles: an operator-enabled snippet storage |
| `template_vmid`, `cpu` | Explicit template ID 100–999999999 and guest CPU model name; `host` is the default example |
| `proxmox_ssh_host`, `proxmox_ssh_user`, `proxmox_ssh_port` | Native host SSH endpoint, root-capable login, port default 22 |
| `proxmox_ssh_private_key_file` | Optional private-key path; blank/omitted uses normal SSH identities |
| `proxmox_api_ca_file` | Optional CA bundle path; blank/omitted uses system trust |

IDs and guest IPv4 addresses must be globally unique within `kdive`, even when a
subset is selected. The complete managed inventory is checked before selection.
Omitted `TARGETS` validates all for this **read-only** operation. Explicit empty,
unknown or duplicate aliases and Ansible patterns such as `all` or `*` fail.
Use literal values for these fields; templated contract values are rejected.
Static IPv4 is the initial contract. The `/31` point-to-point form is accepted;
normal subnet/broadcast addresses, unusable addresses and mismatched gateways fail.
The broad sizing bound prevents malformed inputs; it is not a Proxmox capacity claim.

## KDIVE installation validation sizing

The example allocates **8 vCPUs, 32768 MiB RAM (32 GiB), and 256 GiB root disk**
per guest for [KDIVE #2807](https://github.com/randomparity/kdive/issues/2807).
These are planning defaults for one active installation lane and one nested test
guest, not measured minimums or permission to run every lane concurrently.
CPU and RAM leave room for the host stack, package installation, libguestfs and
the nested guest. Kernel-build concurrency needs a separate measured budget.

The disk budget allows 32 GiB for the OS/tools/container images, 48 GiB for
fixtures and nested guest disks, 48 GiB for working artifacts, and 96 GiB kept
free for KDIVE's default external-boot recovery capacity check (three 32 GiB
activations), leaving 32 GiB for filesystem overhead and headroom. These are
planning budgets; check actual free space before setup and each scenario. Recovery
capacity is free space on the relevant filesystem, not the nominal virtual disk size. See the
[KDIVE installation contract](https://github.com/randomparity/kdive/blob/main/docs/operating/install.md).

Four such VMs total 32 configured vCPUs, 128 GiB RAM and 1 TiB of root disks,
plus templates, auxiliary disks and snapshot growth. On a 24-CPU host a fresh batch
of all four oversubscribes CPU and is admitted with a capacity warning; running all
four lanes under load at once contends for CPU and RAM, so stop idle lanes through
the operator's lifecycle process and leave capacity for Proxmox and other workloads.
Storage admission remains authoritative; thin provisioning is not a substitute for
free capacity. A 4 TB pool is not all available to this lab.

Run the #2807 installation proofs on the dedicated
[clean-only installation guests](snapshots.md#clean-only-installation-guests), not on the `kdive` level,
which is warm-fixture preparation rather than installation evidence (see
[KDIVE installed level](levels.md#kdive-installed-level)).

Ubuntu, Fedora and Rocky provide the three host-family representatives for
#2807. openSUSE remains available for the broader #2803 guest matrix; its presence
does not extend KDIVE's supported host-installation families. Keep nested KVM and
SELinux/AppArmor enforcement enabled. Sizing alone proves neither clean/resettable
ownership nor installation: #2807 still needs documented setup, real provision/boot,
repeat setup and cleanup evidence. Native POWER qualification remains separate.

To change a private inventory, copy it to a new ignored file and update `cores`,
`memory_mib` and `disk_gib`, including any per-host overrides. Validate it offline
before a live plan. **Do not apply changed sizing to an existing guest ID:** sizing
is part of its ownership identity, and provisioning rejects the mismatch instead
of resizing or re-marking it. Preserve the original inventory for existing guests.
Use operator-assigned unused VM IDs and network identities for replacement guests,
or arrange explicitly authorized teardown/recreation of the old disposable guests.
Templates retain their small, unbooted hardware configuration; clone sizing is
independent. Use the [clean snapshot lifecycle](snapshots.md#clean-snapshot-lifecycle) for explicit restore or recreation.

Credential names are checked without reading their environment values. There is no
need to set them for offline checks. Recognizable plaintext API credentials and
Ansible SSH/sudo/su passwords, inline private keys and passphrases are rejected;
unrelated Ansible variables are preserved. Private-key file references remain usable.
This does not sanitize arbitrary private data or sandbox trusted inventory/plugins.
Public-key checks cover encoding/structure, not key ownership or successful login.
Do not put private keys in `ssh_public_keys`.

The same validator is available as a controller-only playbook:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/validate.yml
```

Keep the fixed `-i localhost,` bootstrap: passing private inventory to Ansible's
`-i` parses it before task output protection. Only the validator should load the
private source. It captures parser diagnostics and reports field/rule errors without
input values. The playbook hides task output; run `make validate` for safe details.
