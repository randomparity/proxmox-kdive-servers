# Verified cloud-image templates

Scope: issue #3, `WORK:SCOPE` token `q3-165bc21f`. The operator approved the
native SSH/qm/pvesh design and disposable image inspection. Live imports need
a separate approval of their concrete plan. Decision: [ADR 0002](../../adr/0002-native-template-lifecycle.md).

## Outcome and boundaries

Import pinned official Ubuntu 24.04, Fedora 44, Rocky 10.2 and openSUSE Leap 16.0
x86_64 cloud images as unbooted Proxmox templates. Keep their guest files unchanged;
the verified source digest identifies the original package baseline. Record inspected
OS, package/cloud-init, architecture and firmware evidence with the profiles.
Ubuntu's source lacks qemu-guest-agent; issue #4 owns management-package preparation.
No KDIVE software, guest networking, guest boot/readiness, cloning or snapshots belong here.

Extend the existing inventory validator rather than introducing another inventory.
Its default guest mode retains existing static-address/key/sizing requirements.
Template mode validates explicit template VMIDs, host CPU, optional VLAN, storage,
bridge, API environment references and authenticated SSH connection settings.
It permits unset guest addresses, resolver IPs and VMIDs; provided VMIDs must remain
valid and cannot collide with template IDs. Shared template IDs require identical
template inputs. Validate the complete managed inventory before exact-alias selection.

## Components and contracts

- `vars/images.json` contains fixed HTTPS image URL, vendor checksum URL, SHA256,
  release/build, virtual size, firmware and package-baseline evidence for each profile.
  No mutable `latest` image is accepted. Changing a profile changes its identity.
- `scripts/validate_inventory.py` owns inventory structure and template/guest modes.
  Existing callers retain default guest validation. There is no ownership migration.
- `scripts/templates.py` owns selection, TLS-verified API admission and SSH dispatch.
  A controller-only Ansible role/playbook exposes the same operation as Make.
  Explicit `TARGETS` is mandatory; default execution prints a safe read-only plan.
  `--apply` imports; `--resume` additionally permits the narrow recovery below.
- `scripts/template_host.py` owns native host checks and the serialized lifecycle.
  It receives JSON over SSH stdin and uses argv-based native commands. Require root
  SSH, Linux/x86_64, Python 3, qm, pvesh and qemu-img; install nothing on the host.
  Host CPU is explicitly `host`; virtio networking carries the exact inventory VLAN.
  Templates use 2 cores/2048 MiB, virtio-scsi-pci, serial console, NoCloud and the
  profile's verified firmware. Guest sizing remains issue #4.

The controller first authenticates to the selected API node and storage with the
configured CA/system trust. Failure, malformed response or missing positive evidence
aborts. Native root pvesh supplies authoritative cluster VMID and storage inventory;
API list filtering is never evidence of absence. SSH host-key verification stays on.
The native local node must equal the admitted inventory node before cache/allocation;
valid SSH access to another node is a fatal configuration mismatch.

The template identity hashes the profile, image/baseline digest, template VMID, node,
storage, bridge, VLAN, CPU and fixed template configuration. Store the identity and
phase in the VM description. A ready rerun verifies identity, stopped template status,
configuration, exact managed disks, their ownership and their snapshot-capable storage;
it performs no write. Drift fails with an instruction to select a new explicit VMID.

## Creation and recovery

Hold one nonblocking root-owned host flock for the template VMID through checks,
download, creation and verification. Another run fails busy. Native qm creation
also enforces cluster-wide VMID exclusivity. Supported operations target the configured
single node; an operator must not concurrently edit/migrate these managed resources.

Before allocation, require active image storage of type zfspool or lvmthin, enough
reported free space for the image's virtual disk plus firmware, an existing bridge
with VLAN support when tagged, and an unused cluster VMID. Reject other backends.
Download into a private root-owned cache under a digest-derived filename, bounded by
the pinned image size and a timeout. Verify SHA256 and qemu-img QCOW2 metadata, including
no backing file/encryption/external data file and the pinned virtual size, before import.
Use native qm create with import-from and the exact disk/firmware configuration;
write the ownership/creating marker with creation. Never guess an imported volume name.

Read back configuration/volumes and check native snapshot capability before conversion.
After qm template, verify base-volume ownership, raw format and admitted snapshot-capable
backend plus native clone capability; base volumes themselves are not snapshot targets.
Then mark ready.
Cloud-init is read-only CD-ROM media. Reject extra disks, unused volumes, passthrough,
pending configuration, running state and native task locks.

The wrapper performs no cleanup/deletion; preserve what remains after native handling
and report the observed phase/state without private values. Native qm creation may roll
back its own fresh allocations. Ordinary reruns reject a creating object.
Explicit resume may only finish an owned, stopped,
fully imported object with the exact expected disks/configuration and no active task.
It may convert to template and/or complete the ready marker. Missing/ambiguous disks,
an incomplete import or a foreign marker require operator inspection; no recovery path
deletes a VM or volume, retries an uncertain allocation, or blesses different contents.

## Failure model

1. **Actors/deployments:** Trusted operator-controlled static inventory on supported
   macOS/Linux controllers; one existing x86_64 Proxmox node with root SSH and an
   authenticated audit-capable API token. Cooperating controllers use this same lock.
2. **Assets/invariants:** Unrelated VMs/disks, exact image provenance, truthful readiness,
   API secrets and inventory privacy. A matching ready template is read-only on rerun.
3. **Accepted classes:** Operator/root modifications or migrations during a run are
   outside this exclusive-use deployment. Ambiguous interrupted imports are deliberately
   preserved for inspection instead of automatically repaired. Upstream artifact removal
   fails visibly until an explicit profile/version update. Guest boot is not import proof.
4. **Elsewhere:** Issue #4 owns boot, addresses/DNS, management packages and nested KVM;
   #5 owns snapshots and destructive lifecycle. The operator owns host/network changes.

## Threat model

- **New boundaries:** Vendor/mirror download bytes, authenticated API JSON, SSH output
  and native configuration/volume JSON. Existing trusted YAML inventory is extended.
- **Actors:** Network attackers and faulty/incomplete external services; operators and
  the authenticated root host are trusted. Inventory is not a hostile-code sandbox.
- **Controls:** Verified TLS/SSH host keys, pinned hash and image size, literal field
  validation, argv-based commands, bounded requests, strict JSON shapes, positive
  admission and ownership/configuration checks. Never print raw inventory, credentials,
  API bodies, native stderr, hostnames or addresses; private evidence stays ignored.
- **Out of scope:** Compromised trusted operator/root/vendor signing infrastructure;
  package security qualification and network/DNS administration belong to their owners.

## Success and validation

Offline tests cover each changed validation contract, selection and shared-ID conflicts,
API/SSH failures, image integrity/format/bounds, storage rejection, CPU/VLAN arguments,
foreign IDs, rerun preservation, concurrent lock rejection and bounded partial recovery.
Use real temporary filesystem fixtures and mock only external commands/transport.
Run the same `make check` on controller platforms in CI.

Before pin acceptance, verify full image checksums and inspect OS/architecture, boot
layout, cloud-init datasource configuration and package identity in an isolated local
Linux container. Record versions/digests; no host packages or modules are installed.
After operator apply approval, import all four templates on the designated host, then
repeat the selected operation and verify unchanged identities/configuration/volumes.
Report actual profiles, outcomes and durations. An import does not claim guest readiness.
