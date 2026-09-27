# ADR 0002: Own template identity around native Proxmox operations

## Status

Accepted by the operator's design approval for issue #3 on 2026-09-27.

## Context

Four official cloud images need checksummed import, matching-template preservation,
snapshot-storage admission and safe interrupted-run handling. Ordinary Ansible inventory
already owns target configuration; the user-provided example uses native qm over SSH.

## Decision

Keep inventory validation on the controller and one serialized native lifecycle on the
Proxmox host, driven by a controller-only Ansible entry point. Use Python's standard
library, existing SSH/qm/pvesh/qemu-img, verified API admission and a per-template host lock.
Bind exact source/configuration identity and phase to the created resource. Preserve
matching templates and reject drift. A different version requires a new explicit VMID.

Keep source images unbooted and unmodified, recording their package baseline identity.
Only explicitly requested recovery may finish an owned object whose import/configuration
is already complete. Residual ambiguous allocation remains visible for operator inspection;
there is no automatic resource deletion or replacement command.
Native qm creation may roll back its own fresh allocations on failure.

## Consequences

Root SSH and a readable authenticated API are prerequisites. Supported writable storage
is initially native-snapshot zfspool/lvmthin. Guest preparation remains issue #4.
The lock serializes cooperating invocations on the selected node; operators must not
edit or migrate the same resource concurrently. Source availability is an external
prerequisite, and an interrupted ambiguous import may consume space until inspected.

## Considered & rejected

- **An additional Proxmox Ansible collection.** judgment: it adds dependency and async-task
  integration surface while image inspection, download and serialization still need host
  operations. Existing native commands are adequate for this bounded lifecycle.
- **Automatically replace mismatching templates.** judgment: deleting persisted resources
  is unnecessary for explicit versioning and risks removing another consumer's baseline.
- **Customize and boot templates before marking ready.** judgment: guest preparation and
  readiness belong to issue #4; retaining official image bytes makes baseline identity
  reproducible without installing tools or changing the hypervisor.
- **Reuse the example unchanged.** verified: `proxmox-k3s-cluster` revision
  `049d271c960a86b815742326433baf2305fc4e0f`,
  `ansible/roles/proxmox_template/tasks/main.yml:2-25`, uses a mutable image URL and
  treats any qm-config failure as absence; issue #3 explicitly requires pinned integrity
  and failure-sensitive identity checks.
