# Proxmox KDIVE server infrastructure

This repository prepares clean Linux VMs for
[KDIVE validation](https://github.com/randomparity/kdive/issues/2803).
It provides offline inventory checks and verified, unbooted Proxmox templates for
four Linux families. Guest provisioning, snapshots and KDIVE installation follow separately.

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
It demonstrates four independently selectable profiles; exact image pins and inspected
baselines are recorded in [vars/images.json](vars/images.json):

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
| `fqdn` | Unique lowercase fully qualified guest DNS name, separate from SSH IPv4 |
| `proxmox_api_host`, `proxmox_api_port` | Hostname/IP without URL scheme; port 1–65535, default 8006 |
| `proxmox_node`, `storage`, `bridge` | Literal identifiers; existence is checked later against the lab |
| `api_user_env`, `api_token_id_env`, `api_token_secret_env` | Distinct uppercase environment-variable names, not credentials |
| `ansible_host`, `ipv4_cidr` | Matching static IPv4 host address and address/prefix |
| `gateway`, `dns_servers` | Different usable gateway in the subnet; non-empty IPv4 DNS list |
| `ansible_user`, `ssh_public_keys` | Guest account and non-empty OpenSSH public-key list (Ed25519/RSA/NIST ECDSA) |
| `cores`, `memory_mib`, `disk_gib` | Positive integer sizing, at most 2147483647; no Boolean/string coercion |
| `vlan` | Optional integer 1–4094; propagated to the template NIC |
| `template_vmid`, `cpu` | Explicit template ID 100–999999999 and literal `host` CPU |
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

Credential names are checked without reading their environment values. There is no
need to set them for offline checks. Recognizable plaintext API credentials and
Ansible SSH/sudo/su passwords, inline private keys and passphrases are rejected;
unrelated Ansible variables are preserved. Private-key file references remain usable.
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

## Verified templates

Run on an existing x86_64 Linux Proxmox host with root SSH, Python 3.11+, `qm`,
`pvesh` and `qemu-img`. This path was developed against Proxmox 9.2; it installs
nothing on the host. The API token needs positive node/storage audit visibility
(for example, inherited `PVEAuditor`). Configure a trusted API CA and SSH known-host
entry first. TLS verification and strict SSH host-key checking remain enabled.

Only active `zfspool` and `lvmthin` image storage is admitted: imported root and EFI
disks must support native snapshots before template conversion, and native cloning
after conversion. Base template volumes themselves are not snapshot targets. Require
space for each image's virtual size plus 16 MiB for auxiliary disks, as well as about
2.1 GB under `/var/cache/kdive-templates` for all four compressed sources. A tagged NIC
requires a VLAN-aware bridge or the native conventional-bridge VLAN uplink support.
The operator owns bridge/uplink configuration and external DHCP/DNS administration.
For LVM-thin storage, the native `vgs` command must expose the selected volume group's
extent size. Admission rounds the root and auxiliary reservations to that geometry;
disk readback accepts only bounded, extent-aligned allocations. ZFS retains its native
allocation checks. An unavailable or malformed extent report fails before allocation.

Assign explicit unused template IDs and CPU `host`. Template validation permits
unassigned guest `vmid`, `ansible_host`, `ipv4_cidr`, `gateway` and `dns_servers`;
operator-assigned static IPv4 addresses and resolver IPs are required before guest
provisioning. Guest validation remains strict. A shared template ID requires identical
profile/node/storage/bridge/VLAN/CPU and endpoint inputs throughout the inventory.
Provided guest IDs must be unique and cannot overlap any template ID.

```sh
.venv/bin/python scripts/validate_inventory.py --purpose templates \
  --inventory inventory/private/lab.yml
# Export the three variables named by api_*_env in private inventory.
# If using a trusted local .env file:
set -a
. ./.env
set +a
make templates INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local
# Review the plan, then explicitly import the selected template:
make templates INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local APPLY=1
```

`TARGETS` is mandatory and accepts exact comma-separated aliases. The default is a
read-only live plan: API authentication, authoritative native VMID/storage/network
checks and existing-template inspection, without cache/lock or VM writes. Apply
holds a nonblocking native per-template lock through download, creation and final
verification. Another cooperating run fails busy. Do not concurrently edit or migrate
these resources through another controller or operator session.

The same controller operation is exposed through Ansible:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local \
  .venv/bin/ansible-playbook -i localhost, playbooks/templates.yml
```

Add `APPLY=1` for import. Keep `-i localhost,`; private source parsing stays inside
the protected task. The role uses `no_log`; Make prints safe outcome/identity JSON.
No API credentials travel over SSH. Failures preserve private input values, report
nonzero status and, when observable, the residual ownership phase.

All profiles use OVMF with enrolled secure-boot keys, `host` CPU, two cores, 2048 MiB,
virtio SCSI, serial console and NoCloud media. VLAN tags come directly from inventory.
Images remain unmodified and unbooted. Their full vendor checksum, exact byte length,
QCOW2 format, virtual size and absence of backing/encryption/external data are checked
before import. Matching ready reruns verify description identity, hardware, stopped
state, exact owned disks and native capabilities without changing Proxmox resources.
Image, baseline or template configuration changes require a new explicit VMID;
there is no automatic replacement or deletion command.

An interrupted creation is refused on an ordinary rerun. After inspecting private
host task/configuration state, `APPLY=1 RESUME=1` may finish only a complete, stopped,
owned import with exact configuration/disks and no pending changes or task lock.
Missing or ambiguous allocations require operator inspection. The wrapper never
removes VMs or volumes; native `qm create` may roll back its own fresh allocations.
API/SSH/native failures never establish resource absence.

The source image digest identifies the unchanged package baseline. Inspection used
read-only libguestfs 1.54.1 to verify OS/architecture, EFI fallback boot files, cloud-init
NoCloud modules/effective configuration and package tuples. `packages_sha256` hashes
compact, sorted-key UTF-8 JSON of package objects with `name`, `epoch`, `version`,
`release`, `arch`, sorted by that tuple. Missing tuple values are empty strings.
Management package versions and absences are recorded separately. In particular,
the Ubuntu source lacks `qemu-guest-agent`; downstream preparation owns installing it.
No security enforcement or package state is changed to manufacture import success.

Import/rerun proof establishes template identity and storage eligibility. Guest
provisioning below establishes boot, networking, cloud-init and nested KVM.
[Issue #5](https://github.com/randomparity/proxmox-kdive-servers/issues/5) owns clean
snapshots, restore, test-use coordination and scoped teardown, consuming this identity.
KDIVE owns installation, libvirt/build/debug tools, runners, workload qualification
and external test state. The operator owns host module/reboot and network changes.

## Verified guests

Complete guest inputs, including each unique `fqdn`, before provisioning. The
short first DNS label becomes the guest hostname; the remainder becomes its DNS
search domain. For example, `ubuntu-local.example.invalid` identifies a guest
independently of its static `ansible_host`. External DNS records remain operator
owned. Guest validation also checks template inputs and guest/template ID collisions.

Keep the private inventory directory mode 0700. Provisioning creates its
`known_hosts` file mode 0600 for guest SSH pins. Fresh owned clones use OpenSSH
`accept-new` once on the operator-trusted lab network; privileged preparation and
subsequent access use strict verification. This assumes trustworthy first contact.
Stored keys are never removed/replaced: a mismatch requires operator inspection.
Private keys remain on the controller. Optional `ansible_ssh_private_key_file`
selects the guest identity; otherwise ordinary OpenSSH identities apply.
Inventory paths may contain spaces. Literal `${`, line breaks and NUL are rejected
before allocation to prevent OpenSSH from reinterpreting the pin location.

```sh
# Export API credentials as described above, then inspect the read-only plan:
make provision INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local
# Create only the selected absent, owned full clone and verify its baseline:
make provision INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local APPLY=1
# Reusable read-only verification, including after a separately managed restore:
make verify INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local
```

Use comma-separated exact aliases for multiple instances. All selected batches
are planned before an apply; admission repeats under the native allocation lock.
New cores plus rounded-up maximum of observed busy CPUs and one-minute load must
fit host logical CPUs. New RAM must fit `MemAvailable`; full root/auxiliary disks
must fit reported storage. These are observed admission checks, not dedicated-core
reservations: the operator controls concurrent workloads. Missing capacity,
nesting, source ownership or native evidence fails before allocation. The host is
never reconfigured or rebooted, and there is no TCG fallback.

Native cooperating locks serialize allocations and selected VM/template operations
through readiness. Full clones receive exact hardware/static networking, optional
VLAN, keys, `host` CPU, guest agent, no ballooning, no automatic startup and no
general cloud-init package upgrade. Root filesystem growth is verified after boot.
Before first start, provisioning replaces only the clone's generated IDE cloud-init
seed with a generated `scsi1` seed on its VirtIO SCSI controller. Native removal
frees the old seed volume; creation regenerates it from inventory. Root and EFI
volumes, VM UUID and source templates are preserved and checked. Failure leaves
an inspectable partial; reruns do not resume or repair it.
Only fresh clones receive management preparation: the pinned Ubuntu image needs
`qemu-guest-agent`; the other three already contain it. Existing SSH/sudo/Python/
cloud-init are checked. The installed guest KVM vendor module is loaded and named
in `/etc/modules-load.d/kdive-kvm.conf` for reboot readiness. Missing modules fail;
no kernel, libvirt, KDIVE, build/debug tooling or runners are installed.

Verification checks successful cloud-init, authenticated SSH/noninteractive sudo,
actual OS/release/x86_64, hostname/FQDN/static IPv4, CPU/RAM/root filesystem, active
guest agent plus native ping, and enforcing SELinux or AppArmor. It opens the KVM
API as root, requires version 12 and creates/closes a transient VM descriptor.
This proves capability; KDIVE owns later virtualization permissions and workloads.
Before any guest preparation, read-only checks match the guest DMI UUID to the
owned VM's native SMBIOS UUID and verify its OS and network identity. A stale IP
cannot authorize preparation merely by accepting the configured SSH credential.
The UUID travels privately and is excluded from public evidence.

Matching ready reruns perform verification only: they do not restart a stopped
guest, update packages/keys, resize disks or clear test state. Drift, extra disks,
pending native changes, foreign ownership and preparing-phase objects fail for
inspection. Failed work retains inspectable resources; there is no automatic
delete, resume, repair or recreate command. Ready does not mean test state is clean.
Issue #5 owns clean snapshots and restoration without blessing a used VM as clean.

The equivalent Ansible entrypoints use the same controller operation:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local \
  .venv/bin/ansible-playbook -i localhost, playbooks/provision.yml
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu_local \
  .venv/bin/ansible-playbook -i localhost, playbooks/verify.yml
```

Add `APPLY=1` only for provisioning. Keep `-i localhost,`; private parsing and task
output remain protected. Make emits sanitized identities, observed baseline and
duration JSON without account names, endpoints, addresses or keys.

For KDIVE handoff, supply the private ordinary inventory and guest `known_hosts`
through a private channel. Point its SSH/Ansible `UserKnownHostsFile` at that file;
keep host-key checking enabled, use its normal guest account/identity and guest
Python rather than the controller virtualenv. Assign one exclusive test consumer
per VM. The consumer must release the VM before provisioning verification or later
restore/teardown, and collect external test results/artifacts before release.
Coordinate this explicitly; this repository does not supply a scheduler. Keep
private inventory and SSH pins after worktree cleanup because they identify access
to persistent VMs. Sanitized reports may be shared; raw native/guest logs may not.
