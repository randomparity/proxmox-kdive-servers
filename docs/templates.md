# Verified templates

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
first (see [ADR 0013](adr/0013-lvmthin-level-storage.md), which amends ADR 0007). LVM-thin has no data checksums or compression,
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

Import/rerun proof establishes template identity and storage eligibility. [Guest
provisioning](guests.md) establishes boot, networking, cloud-init and nested KVM.
The [clean snapshot lifecycle](snapshots.md#clean-snapshot-lifecycle) consumes this identity for fresh
capture, selected restore and scoped teardown.
KDIVE owns installation, libvirt/build/debug tools, runners, workload qualification
and external test state. The operator owns host module/reboot and network changes.
