# Proxmox KDIVE server infrastructure

This repository prepares clean Linux VMs for
[KDIVE validation](https://github.com/randomparity/kdive/issues/2803).
It provides offline inventory checks, verified Proxmox templates, and full-clone
guests with verified nested KVM and fresh clean snapshots for four Linux families.
Selected restore and teardown reset managed VM state; KDIVE installation remains separate.

## Quick start: build all four VMs

This workflow creates Ubuntu, Fedora, Rocky Linux and openSUSE templates, then
provisions and verifies one guest from each for KDIVE testing. Complete
[controller setup](#controller-setup) first and check the Proxmox host, storage,
API permissions and SSH prerequisites in [Verified templates](#verified-templates).
The host must also have working nested KVM before guest provisioning.

Create a private inventory if you do not already have one:

```sh
mkdir -p inventory/private
chmod 700 inventory/private
cp -n inventory/example.yml inventory/private/lab.yml
chmod 600 inventory/private/lab.yml
```

Edit `inventory/private/lab.yml` using the [input reference](#private-inventory).
Replace the example endpoints, node/storage/bridge/VLAN, guest account/public key,
FQDNs, static addresses, gateway and DNS servers with your assigned values. Assign
unused guest and template VM IDs and confirm CPU/RAM/disk sizing. Keep the aliases
`ubuntu`, `fedora`, `rocky` and `opensuse` for the commands below.

Select all four and validate the inventory offline before contacting Proxmox:

```sh
export INVENTORY=inventory/private/lab.yml
export TARGETS=ubuntu,fedora,rocky,opensuse
unset APPLY RESUME
make validate
```

`make validate` checks both guest and template inputs, including types, required
fields, address consistency and global uniqueness. It does not require credentials
or prove that the live host has the requested resources.

Export the credentials named by the inventory's three `api_*_env` fields. If you
keep them in a trusted, ignored `.env` file, load it with `set -a; . ./.env; set +a`.
Configure trusted API TLS and Proxmox SSH host keys as described below.

Plan template creation, review the result, then apply it:

```sh
make templates
# After reviewing the plan:
make templates APPLY=1
```

With all templates ready, plan and create the four guests:

```sh
make provision
# After reviewing the plan:
make provision APPLY=1
make verify
```

Plans perform live admission checks; apply repeats admission before creation.
Provisioning verifies each guest's boot, networking, sizing, security enforcement
and nested-KVM baseline, captures a no-RAM `clean` snapshot, then boots and verifies again.
`make verify` repeats verification without provisioning.
These commands prepare the VMs; KDIVE installation is a separate step.

**Capacity:** the example requests 32 vCPUs, 128 GiB RAM and 1 TiB of guest root
disks, plus host headroom, templates and auxiliary disks. A 24-CPU host cannot
admit all four as a fresh batch. On a smaller host, select one distro at a time
(for example, `make provision TARGETS=ubuntu`, then the same command with
`APPLY=1`) and stop idle guests through your normal operator process before
continuing. See [sizing guidance](#kdive-installation-validation-sizing); running
commands sequentially does not free resources held by running guests.

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

### KDIVE installation validation sizing

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
plus templates, auxiliary disks and snapshot growth. A 24-CPU host cannot admit
all four as one fresh batch; run exact targets sequentially, stop an idle lane
through the operator's lifecycle process, and leave capacity for Proxmox and
other workloads. Even two lanes need 64 GiB available RAM plus host headroom.
The existing live admission checks remain authoritative; thin provisioning is
not a substitute for free capacity. A 4 TB pool is not all available to this lab.

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
independent. Use the [clean snapshot lifecycle](#clean-snapshot-lifecycle) for explicit restore or recreation.

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

Choose storage by how levels will be used. On `lvmthin`, restore may move to any level and
back up again with every other snapshot kept. On `zfspool`, rollback is possible only to the
newest snapshot, so a lower level requires the operator to remove every newer level snapshot
first (see ADR 0013, which amends ADR 0007). LVM-thin has no data checksums or compression,
and exhausting thin-pool data or metadata space pauses guests, so monitor pool usage.

Assign explicit unused template IDs. Template CPU/RAM/NIC values are placeholders;
guest CPU, sizing, networking and storage come from inventory. Template validation permits
unassigned guest `vmid`, `ansible_host`, `ipv4_cidr`, `gateway` and `dns_servers`;
operator-assigned static IPv4 addresses and resolver IPs are required before guest
provisioning. Guest validation remains strict. A shared template ID requires identical
profile/node and endpoint inputs throughout the inventory; guest hardware, destination
storage and network settings may differ.
Provided guest IDs must be unique and cannot overlap any template ID.

```sh
.venv/bin/python scripts/validate_inventory.py --purpose templates \
  --inventory inventory/private/lab.yml
# Export the three variables named by api_*_env in private inventory.
# If using a trusted local .env file:
set -a
. ./.env
set +a
make templates INVENTORY=inventory/private/lab.yml TARGETS=ubuntu
# Review the plan, then explicitly import the selected template:
make templates INVENTORY=inventory/private/lab.yml TARGETS=ubuntu APPLY=1
```

`TARGETS` is mandatory and accepts exact comma-separated aliases. The default is a
read-only live plan: API authentication, authoritative native VMID/storage/network
checks and existing-template inspection, without cache/lock or VM writes. Apply
holds a nonblocking native per-template lock through download, creation and final
verification. Another cooperating run fails busy. Do not concurrently edit or migrate
these resources through another controller or operator session.

The same controller operation is exposed through Ansible:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/templates.yml
```

Add `APPLY=1` for import. Keep `-i localhost,`; private source parsing stays inside
the protected task. The role uses `no_log`; Make prints safe outcome/identity JSON.
No API credentials travel over SSH. Failures preserve private input values, report
nonzero status and, when observable, the residual ownership phase.

All profiles use OVMF with enrolled secure-boot keys, `host` CPU, two cores, 2048 MiB,
virtio SCSI, serial console and NoCloud media. New templates have untagged placeholder
NICs. Existing templates retain their original verified bridge, VLAN and storage,
even when the guest inventory requests different values. Template operations never
rewrite source defaults or ownership markers to match guest settings.
Images remain unmodified and unbooted. Their full vendor checksum, exact byte length,
QCOW2 format, virtual size and absence of backing/encryption/external data are checked
before import. Matching ready reruns verify description identity, hardware, stopped
state, exact owned disks and native capabilities without changing Proxmox resources.
Source image, baseline or actual template drift requires a new explicit VMID;
there is no automatic replacement or deletion command.

An interrupted creation is refused on an ordinary rerun. After inspecting private
host task/configuration state, `APPLY=1 RESUME=1` may finish only a complete, stopped,
owned import with exact configuration/disks and no pending changes or task lock.
Missing or ambiguous allocations require operator inspection. The wrapper never
removes VMs or volumes; native `qm create` may roll back its own fresh allocations.
API/SSH/native failures never establish resource absence.

The source image digest identifies the unchanged package baseline. Inspection used
read-only libguestfs (version recorded per image) to verify OS/architecture, EFI fallback boot files, cloud-init
NoCloud modules/effective configuration and package tuples. `packages_sha256` hashes
compact, sorted-key UTF-8 JSON of package objects with `name`, `epoch`, `version`,
`release`, `arch`, sorted by that tuple. Missing tuple values are empty strings.
Management package versions and absences are recorded separately. In particular,
the Ubuntu source lacks `qemu-guest-agent`; downstream preparation owns installing it.
No security enforcement or package state is changed to manufacture import success.

Ubuntu uses the Ubuntu 26.04 release-20260918 image, with its native Python 3.14 package
family. Changing from the previous Ubuntu 24.04 pin changes template and guest identities;
use separately assigned replacement resources and rebuild the snapshot chain. Existing
captures do not become compatible by editing their metadata.

Ubuntu 26.04's cloud-init refuses to rename an already active interface, and the network
config Proxmox generates always renames it. Ubuntu guests therefore take their network
config from a product-written snippet that matches the cloned NIC by MAC and never renames
it; user, keys and hostname remain native. Before provisioning Ubuntu, the operator enables
`snippets` content on an active directory storage (for example
`pvesm set local --content <existing>,snippets`) whose `snippets/` directory exists, is owned
by root and is not group- or world-writable; the tools never change storage configuration.
Name that storage in `cloudinit_snippet_storage`. Inventory validation covers every managed
host, so an inventory holding an Ubuntu host must add the field before any guest operation.
The field changes Ubuntu guest identities; Ubuntu guests provisioned without it are no longer
managed by these tools, while other profiles' identities are unchanged.

Import/rerun proof establishes template identity and storage eligibility. Guest
provisioning below establishes boot, networking, cloud-init and nested KVM.
The [clean snapshot lifecycle](#clean-snapshot-lifecycle) consumes this identity for fresh
capture, selected restore and scoped teardown.
KDIVE owns installation, libvirt/build/debug tools, runners, workload qualification
and external test state. The operator owns host module/reboot and network changes.

## Verified guests

Complete guest inputs, including each unique `fqdn`, before provisioning. The
short first DNS label becomes the guest hostname; the remainder becomes its DNS
search domain. For example, `ubuntu.example.invalid` identifies a guest
independently of its static `ansible_host`. External DNS records remain operator
owned. Guest validation also checks template inputs and guest/template ID collisions.

Guest provisioning reads the selected source's original VLAN through the authenticated
API and revalidates its exact immutable identity and configuration through native SSH.
The guest's requested VLAN is bound separately and applied while preserving the cloned
NIC's MAC address. Tagged and untagged guests can share a source template without
changing that template. The API token therefore needs selected-template configuration
audit access as well as node/storage visibility. Template operations resolve the same original source settings, so the same guest
inventory works for both template checks and provisioning.

Keep the private inventory directory mode 0700. Provisioning creates its
`known_hosts` file mode 0600 for guest SSH pins. Fresh owned clones use OpenSSH
`accept-new` once on the operator-trusted lab network; privileged preparation and
subsequent access use strict verification. This assumes trustworthy first contact.
Stored keys are never removed/replaced: a mismatch requires operator inspection.

Cloud-init must finish with no errors. One exact compatibility advisory is accepted:
Proxmox 9.2 generates a scalar `user` value, which cloud-init deprecates until its
scheduled removal in 27.2. Status must be `done`; every recoverable notice must be
that advisory. All other notices and failures are rejected. This explains the
documented [cloud-init exit code 2](https://docs.cloud-init.io/en/latest/explanation/return_codes.html)
seen with the pinned Fedora image; it does not change the host generator or suppress
guest logging. Template/host lifecycle owners must address the format before removal.
Private keys remain on the controller. Optional `ansible_ssh_private_key_file`
selects the guest identity; otherwise ordinary OpenSSH identities apply.
Inventory paths may contain spaces. Literal `${`, line breaks and NUL are rejected
before allocation to prevent OpenSSH from reinterpreting the pin location.

```sh
# Export API credentials as described above, then inspect the read-only plan:
make provision INVENTORY=inventory/private/lab.yml TARGETS=ubuntu
# Create only the selected absent, owned full clone and verify its baseline:
make provision INVENTORY=inventory/private/lab.yml TARGETS=ubuntu APPLY=1
# Reusable read-only verification, including after a separately managed restore:
make verify INVENTORY=inventory/private/lab.yml TARGETS=ubuntu
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
VLAN, NIC model/queues, keys, selected CPU model, guest agent and balloon device,
with no automatic startup or general cloud-init package upgrade. Root filesystem growth is verified after boot.
Guest configuration is applied explicitly before boot: template defaults do not
select the guest CPU model, NIC, destination bridge/storage or cloud-init values.
The CPU model must expose working nested KVM; there is no emulation fallback.

Ballooning is enabled by default with target equal to configured RAM. Set
`balloon_mib` lower to allow reclaiming memory, or set it to 0 to disable the device.
Admission still reserves maximum configured RAM; ballooning does not authorize
memory overcommit. Verification requires a bound guest `virtio_balloon` driver when
enabled and checks observed RAM against the configured balloon target and maximum.
NIC queues use the native default when omitted; explicit `nic_queues` is verified
exactly and requires `nic_model: virtio`. NIC model names follow Proxmox's supported
models, including `virtio`, `e1000`, `e1000e` and `vmxnet3`.
Before first start, provisioning replaces only the clone's generated IDE cloud-init
seed with a generated `scsi1` seed on its VirtIO SCSI controller. Native removal
frees the old seed volume; creation regenerates it from inventory. Root and EFI
volumes, VM UUID and source templates are preserved and checked. Failure leaves
an inspectable partial; reruns do not resume or repair it.
Ubuntu clones first receive an immutable network seed `kdive-net-<vmid>-<sha256>.yaml` in
the snippet storage, referenced as `cicustom: network=<storage>:snippets/<name>`. Its name is
the SHA-256 of its bytes, and the bytes carry the guest identity, so the file is never
rewritten; an existing file is reused only when byte-identical, root owned, singly linked
and not group- or world-writable. Provisioning, verify, level preparation, restore and every
start re-derive the seed from the NIC MAC and inventory and require the exact reference and
bytes; a missing or changed seed stops before start for operator inspection.
Only fresh clones receive management preparation: the pinned Ubuntu image needs
`qemu-guest-agent`; the other three already contain it. Existing SSH/sudo/Python/
cloud-init are checked. The installed guest KVM vendor module is loaded and named
in `/etc/modules-load.d/kdive-kvm.conf` for reboot readiness.
The pinned openSUSE Minimal-VM image contains `kernel-default-base` without KVM
modules. Fresh openSUSE provisioning replaces that exact base package
`6.12.0-160000.38.1.160000.2.24` with signed `kernel-default 6.12.0-160000.38.1`
and `ucode-intel 20260812-160000.1.1`. It verifies the two RPM identities/signatures
and the actual native transaction before its sole confirmation. No other package
action is accepted. Normal vendor RPM scriptlets update guest boot artifacts;
the running kernel build and kernel binary must remain unchanged. SELinux and
integrity lockdown stay enforced. Other missing vendor KVM modules fail.
After initial verification, fresh openSUSE receives one graceful authenticated
`systemctl --no-block reboot` request. Strict reconnect must prove the same VM
UUID, changed boot identity and full baseline again before ready marking. A
failure retains the partial. An SSH disconnect during the one reboot request may
carry no response or the complete reboot acknowledgement; both require the same
strict post-boot proof. Invalid responses stop for inspection.
There is no forced power fallback, reboot retry or automatic recovery. Ready reruns remain read-only.
`clean` provisioning installs no libvirt, KDIVE, build/debug tooling or runners.

Verification checks successful cloud-init, authenticated SSH/noninteractive sudo,
actual OS/release/x86_64, hostname/FQDN/static IPv4, CPU/RAM/root filesystem, active
guest agent plus native ping, and enforcing SELinux or AppArmor. It opens the KVM
API as root, requires version 12 and creates/closes a transient VM descriptor.
This proves capability; KDIVE owns later virtualization permissions and workloads.
Before any guest preparation, read-only checks match the guest DMI UUID to the
owned VM's native SMBIOS UUID and verify its OS and network identity. A stale IP
cannot authorize preparation merely by accepting the configured SSH credential.
The UUID and boot identity travel privately and are excluded from public evidence.
RAM evidence reports usable `memory_bytes` and measured `crash_reserved_bytes`
separately. Allocation proof requires their sum to be at least 90% of configured
RAM (or the positive balloon target), without changing the image's crash-kernel
settings. Only the native sysfs reservation counts, capped at 512 MiB and one quarter of configured RAM; their
sum cannot exceed maximum configured RAM. Ubuntu guests allow up to 1 GiB (still at most one
quarter of RAM) because the Ubuntu kdump tools installed by KDIVE reserve 1 GiB from 32 GiB. Missing crash-reservation support counts as
zero; malformed or unreadable evidence fails.

Matching ready reruns perform verification only: they do not restart a stopped
guest, update packages/keys, resize disks or clear test state. Drift, extra disks,
pending native changes, foreign ownership and preparing-phase objects fail for
inspection. Failed work retains inspectable resources; there is no automatic
delete, resume or repair. Ready does not mean current test state is clean. A missing or
stale `clean` snapshot fails rather than blessing a used VM; explicitly selected teardown
and fresh provisioning is the recreation path.

The equivalent Ansible entrypoints use the same controller operation:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/provision.yml
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
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


## Clean snapshot lifecycle

Fresh provisioning captures `clean` only after management preparation and baseline verification.
It gracefully shuts down, snapshots guest-writable root and EFI disks without RAM, boots, and
repeats strict SSH, OS/configuration, sizing, guest-agent, security-enforcement and KVM checks.
The cloud-init CD-ROM is guest-read-only: its volume reference and generating configuration are
bound, while native snapshotting skips its contents. Additional disks and unsupported storage
fail admission. The snapshot binds the existing image/guest identity and a stable native
configuration fingerprint; native generation-ID rotation on rollback is expected, while SMBIOS
identity remains bound. Output includes snapshot name, identity, configuration hash and timestamp.

Ordinary provision/verify reruns neither replace the snapshot nor reset current guest files.
Missing, stale, partial, RAM-state or mismatched snapshots fail. There is no command to capture
an already-used guest as clean. Do not manually rename another snapshot to `clean`: its metadata
will not satisfy the fresh baseline contract.

Restore and teardown default to read-only live plans. Select exact aliases; collect test results,
release external services/artifacts, and stop other consumers before applying. `EXCLUSIVE=1`
attests that release; cooperative locks serialize these tools but cannot fence a different
operator or application writing to the VM. KDIVE owns external state and result collection.

```sh
export INVENTORY=inventory/private/lab.yml
export TARGETS=ubuntu
unset APPLY CONFIRM EXCLUSIVE
make restore
# After reviewing the selected plan and releasing the consumer:
make restore APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1
```

Restore verifies the existing baseline before stopping a running guest, rolls back without RAM,
boots, and rechecks the full read-only baseline. It also accepts an already-stopped guest. The
`CONFIRM` value must match `TARGETS` exactly, including order; wildcards/empty/duplicate/unknown
aliases fail. A mismatch fails before API/SSH access.

Teardown permanently removes only selected owned VMs and their inspected snapshots/disks. It
preserves shared templates and unselected VMs. It accepts a complete matching older owned guest
without `clean`, enabling intentional replacement; absent selected guests are an unchanged result.
Foreign ownership, changed configuration, unknown disks or snapshot references, incomplete
allocations and native task locks require inspection instead of broader deletion.
Ubuntu teardown checks the seed reference but not its bytes. After the VM is gone it deletes
the seed only when every VM and container configuration in the cluster (current, pending and
snapshot sections) reads cleanly and none names it; otherwise the file is retained and the
command fails after removal so the operator can inspect it. A rerun reports the guest absent
and does not revisit the retained file.

```sh
make teardown
# This deletes the selected VM and its existing snapshots permanently:
make teardown APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1
# Recreate a verified fresh guest and clean baseline after reviewing SSH pins:
make provision
make provision APPLY=1
```

Recreation changes guest SSH keys. Preserve the private inventory/pin file, confirm the old
selected VM was removed, then remove only its obsolete address entry from that private
`known_hosts` using your trusted SSH administration process. The tools never replace pins or
disable host-key checking. Fresh first contact still requires a trusted lab network.

Equivalent separate playbooks retain protected private parsing/output:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/restore.yml
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1 \
  .venv/bin/ansible-playbook -i localhost, playbooks/teardown.yml
```

Commands await native task completion. Shutdown gets 180 seconds with no forced-stop fallback;
snapshot, rollback and deletion get 1800 seconds; startup SSH gets 600 seconds. An error or
transport timeout is not success or permission to retry: inspect native task/configuration state
privately first. A task may outlive its disconnected controller. Never clear its lock to bypass
inspection. Failed work retains its observed state; complete owned guests can be explicitly
torn down after tasks finish, while incomplete/ambiguous allocations need manual recovery.
Snapshots cover VM state only, not external services, DNS, backups or test artifact storage.

### Clean-only installation guests

KDIVE #2807 installation proofs need a guest that can always return to `clean`. On ZFS storage a
guest carrying `toolchain`, `kernel-src` or `kdive` snapshots cannot be restored to `clean`,
because newer snapshots block rollback (see [Operator-captured levels](#operator-captured-levels)),
and removing them would destroy the warm fixtures the levels exist to provide. Keep the two roles
on separate guests.

Give each host family (Ubuntu, Fedora, Rocky) a dedicated clean-only guest whose only snapshot
is `clean`, and never capture a higher level on it. The example inventory names them
`ubuntu-install`, `fedora-install` and `rocky-install`; they reuse the `ubuntu`, `fedora` and
`rocky` profiles and templates with their own VM IDs and network identities, so no level or
restore semantics change. The `ubuntu`, `fedora` and `rocky` aliases stay the full-chain
warm-fixture guests.

Reset cycle for each proof:

1. Restore `clean` before the proof, so it starts from the verified baseline.
2. Run the installation proof and collect its results.
3. Restore `clean` again afterwards, after releasing external services and artifacts.
4. When a fresh guest identity is wanted, run `make teardown` then `make provision` instead.
   Recreation changes guest SSH keys; follow the pin handling under
   [Clean snapshot lifecycle](#clean-snapshot-lifecycle).

```sh
export TARGETS=ubuntu-install
make restore APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1
```

The dedicated guests are additional VMs beyond the warm-fixture guests. Each uses the sizing in
[KDIVE installation validation sizing](#kdive-installation-validation-sizing), so run them
sequentially, one exact target at a time, under the existing live admission checks. All seven
example aliases together would configure 56 vCPUs, 224 GiB RAM and 1.75 TiB of root disks, so
always set `TARGETS`; an unset value selects every alias.

## Operator-captured levels

The level contract supports an ordered, closed registry above `clean`. The implemented
`toolchain` level prepares developer headers and tools, Docker Engine with Compose, local
libvirt/QEMU, and operator `uv`/`just`. Its parent is `clean`. `kernel-src` adds a pinned,
shallow, detached, unbuilt Linux source checkout above `toolchain`. `kdive` delegates installation
to a pinned upstream checkout above `kernel-src`. There is no configurable plugin loader or
test-level switch.

`LEVEL` defaults to `clean` for `make verify` and `make restore`. Existing clean metadata and
successful output stay unchanged. A higher level binds its exact native configuration, guest
identity, declared parent, digest of the parent's complete metadata, and recorded content.
Verification walks every ancestor, rejects RAM/incomplete snapshots, compares Proxmox ancestry,
checks root-owned 0644 manifests in `/var/lib/kdive-levels/`, and runs each guest check hook.
Recorded pins describe the snapshot; changing configured pins does not silently invalidate it.
Replacing a parent's metadata invalidates descendants and requires re-preparation.

For an installed higher level, `make level LEVEL=<registered-name> TARGETS=<alias>` produces a
read-only plan. Applying that reviewed plan requires `APPLY=1`, `CONFIRM` exactly matching
`TARGETS`, and `EXCLUSIVE=1` after consumers release the guest. The running guest must verify
at its parent, its native current parent must match, and the target snapshot must not exist.
Preparation verifies content and its manifest, then gracefully shuts down with no force-stop
fallback. Only after stopped-state validation does it print `READY TO SNAPSHOT <level> for
<alias>` and one JSON line containing the exact `level` name and `metadata` object.

The operator then captures a no-RAM snapshot using that exact name and the compact JSON
serialization of `metadata` as its description, through the Proxmox UI or `qm snapshot` with
`--vmstate 0 --description '<metadata JSON>'`. Start the guest before `make verify LEVEL=<name>`.
The tool never creates, renames or deletes higher-level snapshots. A failed preparation leaves
inspectable state and emits no READY; inspect it and restore the parent before another attempt.
The current parent plus content checks prove the declared contract, not every unrelated disk byte.

Restore admits native chain metadata before stopping or rolling back, so a stopped guest or a
guest with damaged disk contents can recover. After rollback it boots and checks guest content.
On ZFS storage, newer snapshots block rollback to a lower level, including `clean`; admission
fails before shutdown and asks the operator to inspect/remove newer snapshots. It never removes
them automatically. The same check applies to the read-only restore plan. `lvmthin` storage
has no such restriction; see the storage guidance above. Cooperative locks and
`EXCLUSIVE` cannot fence a separate privileged operator changing snapshots outside this tool.

### Toolchain preparation

```sh
make level LEVEL=toolchain TARGETS=ubuntu
make level LEVEL=toolchain TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
# Capture the emitted stopped, no-RAM snapshot using its exact metadata, then start the guest.
make verify LEVEL=toolchain TARGETS=ubuntu
make restore LEVEL=toolchain TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
```

Preparation installs distro packages, enables Docker and local libvirt, adds the configured
operator to `docker`, `kvm` and `libvirt`, and makes operator-owned `~/src` and its home safe
from group/other writes. It refuses symlinks, foreign ownership and unsafe system ancestors.
Docker membership grants broad guest privileges. Security enforcement stays enabled.
Operator login checks exercise GNU tools, Bash >=4.4, native build tools, headers, QEMU,
Docker/Compose and system libvirt, including operator Docker access. `realpath` may also be
uutils coreutils, which Ubuntu 26.04 installs by default; `find` and `grep` must be GNU, and
any other `realpath` provider fails preparation and verification.

The operator gets pinned `uv 0.12.19` from Astral and `rust-just 1.58.0` from PyPI with
`~/.local/bin` on the login PATH. The manifest records distro/release, critical tool versions,
and the hash of the sorted installed package inventory at capture. Additional descendant
packages are allowed; changing recorded critical versions fails verification.

Rocky preparation adds the operator-approved Docker stable RHEL repository and verifies its
signing key before import, preserving TLS and RPM signature checks. It refuses conflicting
configuration rather than replacing it. Rocky also uses stock RPM-verified CRB sources
(per toolchain transaction) and signed `epel-release` bootstrap from Rocky Extras for
EPEL 10.2 packages. DNF transactions select only BaseOS, AppStream, Extras, CRB, stable
EPEL and the approved Docker source, with TLS and package signature checks enabled.
Altered repository/key files, unowned EPEL configuration and duplicate source IDs are
refused. Source selection is reproducible; signed package revisions can advance and
are recorded in the installed inventory at capture. Rocky also installs and checks
`kernel-modules-extra` matching the running kernel, which pulls its matching modules;
installing only the newest kernel modules leaves the baseline kernel without Docker's
required `xt_addrtype` networking module.

Rocky gets `shfmt v3.14.1` from the
[official linux_amd64 release](https://github.com/mvdan/sh/releases/tag/v3.14.1),
with SHA-256 `76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf`.
The download uses HTTPS and a pinned checksum; no detached signature is claimed.
Preparation preserves conflicting `/usr/local/bin/shfmt` content. Verification checks
its digest, ownership/mode, operator login resolution and version instead of requiring
a Rocky shfmt RPM. Other distributions retain their packaged shfmt requirement.

openSUSE remains best-effort; this level does not imply KDIVE worker-host support. Missing packages
or service failures stop preparation for inspection; there is no automatic rollback.

Native proof on 2026-09-28: Ubuntu 24.04, Fedora 44 and openSUSE Leap 16.0 reached READY.
Their operator-captured snapshots passed verify and restore. Hiding `just` and removing the operator's `docker`
membership each produced an actionable verification failure; restoring the snapshot repaired
each fault and re-verified the level. AppArmor/SELinux baseline checks stayed enforcing. All three guests finished stopped with
`clean` and `toolchain` retained. openSUSE installs `polkit` explicitly so its existing libvirt
group authorization policy works without enabling recommended packages wholesale.

Rocky 10.2 native proof on 2026-09-28 used verifier commit
`1ac2316a5bc4274dff1f5848294b205858aed1c1`: clean restore → prepare → READY →
operator no-RAM `toolchain` capture → verify/restore passed. CRB supplied
`libvirt-devel 11.10.0-12.4.el10_2`; EPEL 10.2 supplied `ShellCheck 0.10.0-3.el10_0`,
bootstrapped by Rocky-signed `epel-release 10-7.el10_1`. Captured versions were
Docker 29.8.1, libvirt 11.10.0, uv 0.12.19 and just 1.58.0; shfmt's installed digest
and v3.14.1 version matched the approved binary. The initial live attempt exposed the
running-kernel module gap described above; the corrected clean-to-READY retry passed.

Renaming shfmt produced `Toolchain shfmt differs or unavailable; restore toolchain or re-prepare`.
Removing the operator's docker membership independently produced
`Toolchain operator missing docker group; restore toolchain or re-prepare`.
Each fault was followed by a successful snapshot restore and re-verification.
The final guest retained `clean` and `toolchain`, stopped with SELinux enforcing.
Rocky's `kernel-src` and `kdive` levels remain unprepared and unverified.

Measured snapshot allocation above `clean`:

| Profile | New blocks since clean (MiB) | Referenced-size increase (MiB) |
| --- | ---: | ---: |
| Ubuntu 24.04 | 820.29 | 786.87 |
| Fedora 44 | 1751.84 | 1667.27 |
| openSUSE Leap 16.0 | 1005.34 | 867.96 |
| Rocky 10.2 | 1593.07 | 1509.36 |

These are historical ZFS measurements: native ZFS `written@clean` and `referenced` differences at the captured toolchain
snapshot, summed across root and EFI volumes (1 MiB = 1,048,576 bytes). They exclude disk
reservations and are measurements of these package versions, not capacity guarantees.


### Kernel source level

`vars/kernel-source.json` supplies exactly `repo` and `ref` for preparation. The defaults
are the upstream stable HTTPS repository and `v6.9`, matching KDIVE's fetch helper. Edit
these inputs before preparing a new snapshot to select a different ref. Repositories must
use credential-free HTTPS; refs must be a full lowercase 40-hex commit or an unambiguous
exact tag/branch. Preparation resolves the ref once and records `repo`, `ref` and `commit`
in `/var/lib/kdive-levels/kernel-src.json` and the emitted snapshot metadata.

```sh
make level LEVEL=kernel-src TARGETS=ubuntu
make level LEVEL=kernel-src TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
# Capture the stopped guest with the exact emitted name/description and no RAM.
make verify LEVEL=kernel-src TARGETS=ubuntu
make restore LEVEL=kernel-src TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
```

The existing `toolchain` parent must verify first. Git runs as the operator account at
`~/src/linux`; preparation refuses an existing mismatching checkout without resetting or
removing it. A failed initial fetch can leave a partial tree: inspect it before retrying,
and let the operator decide its disposition. No package changes or kernel builds occur.
Verification uses recorded metadata offline; changing inputs or moving a remote tag does
not invalidate a captured level. It checks the real checkout and `.git` directories,
recursive ownership, detached matching HEAD, depth-one shallow history and clean status,
including ignored build products such as `.config` and object files. Deepening history,
configuring or building in this tree makes verification fail. Restore the captured level
or deliberately prepare a replacement; the tool does not delete trees or snapshots.

Native proof on 2026-09-28 used Linux `v6.9` commit
`a38297e3fb012ddfa7ce0321a7e5a8daeb1872b6` on Ubuntu 24.04 and Fedora 44.
Both reached READY and passed verification of the operator-captured snapshot. An
untracked file, ignored `.config`, wrong HEAD and wrong file owner each failed verification;
restoring `kernel-src` repaired each isolated fault and passed an independent reverify.
Both guests finished stopped with `clean`, `toolchain` and `kernel-src` retained.

Measured root-volume allocation above `toolchain` at READY:

| Profile | New blocks since toolchain (MiB) | Referenced-size increase (MiB) |
| --- | ---: | ---: |
| Ubuntu 24.04 | 934.66 | 785.68 |
| Fedora 44 | 705.05 | 537.04 |

These are historical ZFS measurements: native ZFS `written@toolchain` and `referenced` differences against the
parent snapshot (1 MiB = 1,048,576 bytes), excluding disk reservations. They measure
these prepared trees and guest writes, not a capacity guarantee.

### KDIVE installed level

`vars/kdive-source.json` selects an HTTPS repository and full commit for preparation only.
The checkout lives at the operator's `~/src/kdive`; mismatching or dirty existing trees are
preserved and refused. Preparation delegates `just setup` and the upstream local-libvirt
host play. It requires installed Python 3.14; the upstream play must provide its native
`guestfs` binding. Ubuntu 26.04 and Fedora 44 have completed proofs.
openSUSE is excluded;
Rocky has a verified toolchain snapshot; its kernel-source and KDIVE preparation remain unverified.

```sh
make level LEVEL=kdive TARGETS=fedora
make level LEVEL=kdive TARGETS=fedora APPLY=1 CONFIRM=fedora EXCLUSIVE=1
# Take the exact no-RAM snapshot only after READY, using its emitted metadata.
make verify LEVEL=kdive TARGETS=fedora
make restore LEVEL=kdive TARGETS=fedora APPLY=1 CONFIRM=fedora EXCLUSIVE=1
```

Preparation requires an exclusive guest with no existing Docker containers/volumes or
installed KDIVE lifecycle state. It creates an owned disposable local backend project;
Postgres, SeaweedFS and OIDC publish only on guest loopback. There is no external witness
DSN input. Captured witness credentials belong only to this disposable guest. Successful
stop preserves local volumes; it independently verifies no host daemons or workers remain.
Installer failures retain root-private logs under `/var/lib/kdive-levels/kdive-state` and
withhold READY. Inspect them privately; they can contain credentials. Automatic retries do
not replace partial installations, snapshots, or ancestors. Preparation may take up to four
hours. Any critical ancestor tool-version drift fails verification.

The manifest and snapshot content record `kdive_sha`, `kernel_commit`, repository and a hash
of non-secret play inputs. Verification also reports the recorded `kdive_sha`; it does not
compare against a newly selected default. Verification expects the stack stopped, with its
lifecycle socket enabled. To use the captured deployment after restore, start a clean shell
as its operator with the same Compose identity and loopback override:

```sh
cd ~/src/kdive
env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" \
  PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 \
  DOCKER_HOST=unix:///var/run/docker.sock COMPOSE_PROJECT_NAME=kdive-level \
  COMPOSE_FILE="$PWD/docker-compose.yml:/var/lib/kdive-levels/kdive-state/compose.yml" \
  UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 bash --noprofile --norc
scripts/live-stack/stack-services.sh --skip-obs
source scripts/live-stack/env.sh
export KDIVE_STACK_SKEW_POLICY=strict
uv run --no-sync python -m pytest \
  tests/integration/test_live_stack.py::test_viewer_denied_operator_op_over_the_wire \
  -m live_stack --strict-markers -q
scripts/live-stack/stack-down.sh
```

Use this same environment for start and stop; plain upstream commands from another login
can select different volumes or publications. Require exactly one pass and no skips for this
bounded real HTTP authorization proof. It is not a nested VM provisioning or kdump proof.
Before capturing, confirm daemon/worker stop and gracefully shut down the guest.
Boot the guest before `make verify`; keep the KDIVE stack stopped during verification.

KDIVE preparation fetches genuine upstream `main` into `origin/main` for the upstream setup
schema guard and requires that ref to equal the explicitly approved source pin. If main
has advanced, preparation stops for source selection; it never advances the pin or bypasses
the guard. Verification and restore use recorded installed content without this network check.

KDIVE preparation installs `python3-packaging` from the guest's configured repositories
and checks its import in `/usr/bin/python3` before invoking the host play. This is the
Ansible pip module's interpreter prerequisite, owned by this integration; it does not
change the ancestor toolchain package contract or replace upstream installer tasks.

Fedora 44 native proof used KDIVE commit
`dffce52ab48227ba7b8807824e1f79bb96dc18f6`. Upstream setup, host installation,
stack start and clean stop completed. A final systemd template-query defect was corrected;
the existing manifest and full parent chain were then audited through normal verification
and the unchanged native stop/READY handshake, without replaying installation. The operator
captured `kdive` without RAM. Ordinary verification and restore passed, followed by exactly
one passing HTTP authorization test with zero skips and strict stack revision checking.
No setup or Ansible step ran after restore. Final verification passed and the guest stopped.

Restore took 90.271 seconds; the test started 41.132 seconds after restore completed, or
131.402 seconds after restore began (controller-observed test-start marker). Historical ZFS measurement: native ZFS
allocation above `kernel-src` was 6,853.27 MiB written and 6,125.94 MiB additional referenced
data. These measurements include the explicitly preserved setup attempts and are not a
minimal clean-install size. The corrected final verifier was proven against that installation;
the entire fresh-install command was not replayed after the query-only correction.

Ubuntu 26.04 native proof on 2026-09-29 used KDIVE commit
`8182457399c25f1fd180806a10a0014d1877ed2b` on a fresh replacement guest provisioned with the
MAC-matched network seed. The operator captured `clean`, `toolchain`, `kernel-src` and
`kdive` without RAM after each READY; each level was verified and restored. The installed
lifecycle interpreters (the lifecycle service virtual environment and the KDIVE project
environment, both Python 3.14.4) import the native `guestfs` binding from Ubuntu's
`python3-guestfs`. After `kdive` restore, exactly one HTTP authorization test passed with zero
skips under strict stack revision checking; its only warning reported the Kubernetes-only
lifecycle witness as not deployed. No setup or Ansible step ran after restore. The stack
stopped with no containers or workers left, final verification passed and the guest stopped.

Restore took 95.071 seconds; the test started 39.316 seconds after restore completed, or
134.387 seconds after restore began (guest clock test-start marker). Historical ZFS measurement: native ZFS allocation
above `kernel-src` was 9,245.50 MiB written and 9,152.84 MiB additional referenced data for
a single clean installation.
