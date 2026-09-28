# Clean snapshot lifecycle

Scope: issue #5, charter `q5-84e2c790`; full-spec, L, fixed denominator 1000.
Operator approved exclusions and interface/metadata design on 2026-09-28.
[ADR 0006](../../adr/0006-clean-snapshot-lifecycle.md) records the contract.

## Outcome and scope

Extend existing controller/native guest ownership; no ownership move or second state store.
After fresh management preparation and verification, gracefully shut down and capture `clean`
without RAM, then boot and repeat read-only readiness. Source image and guest identity remain
unchanged. Baseline snapshot metadata binds that identity and normalized native configuration.
Existing guests without matching baseline fail ordinary provision/verify/restore; explicit
selected teardown then provisioning is the recreate path. Existing snapshots are never adopted.

KDIVE owns installation/tooling/workloads/provider assertions and external state cleanup;
KDIVE maintainers own POWER qualification/catalog; template owners own additional distros/images;
the operator owns host/network/DNS/backup changes; external orchestration owns allocation.
No scheduler, new inventory schema, automatic snapshot replacement, or silent guest adoption.

## Interface and flow

`make restore` and `make teardown` plus separate playbooks run read-only plans by default.
`APPLY=1 CONFIRM=<exact TARGETS string> EXCLUSIVE=1` is required for their mutations. The flags
attest the operator has exclusively assigned the selected VMs and collected/released external
services/artifacts; cooperative locks cannot prove that an unrelated consumer has stopped.
Validation of confirmation and exact aliases precedes API/SSH. Native RPC independently requires
Boolean confirmation and exclusivity for destructive modes. Ordinary provision remains APPLY=1.

All native mutating/verification sessions hold existing global allocation and sorted template/VM
locks through final acknowledgement; read-only plans create no locks. Requests are admitted as a
whole batch before mutation, rechecked under locks; cross-node groups are preplanned first.
Existing resources must be selected owned QEMU guests with exact request identity, no pending
configuration/task locks, admitted storage, and precisely owned root/EFI/cloud-init disks.
For restore accept running or stopped guests; reject host configuration drift before shutdown.
For teardown accept ready or preparing matching guests only when their managed configuration and
complete disks are valid; incomplete allocations require manual inspection, never broad cleanup.
Teardown does not require clean baseline, allowing explicit replacement of older verified guests.

Fresh apply: clone → prepare/verify (including existing openSUSE reboot) → graceful shutdown
(`qm shutdown --timeout 180 --forceStop 0`) → inspect stopped/config unchanged → require no
existing snapshots → `qm snapshot clean --vmstate 0 --description <metadata>` → read back
snapshot and all disk/configuration identities → `qm start` → controller strict SSH/readiness
acknowledgement → ready outcome. The second boot requires a changed guest boot ID.
No separate command may capture a baseline from an existing guest.

Restore: validate baseline/configuration before stop → graceful shutdown if running → revalidate
stopped baseline → `qm rollback clean --start 0` → validate stopped configuration/baseline →
`qm start` → controller strict SSH/read-only guest verification and native agent ping → outcome.
Fresh preparation is never invoked during restore. Startup reconnect budget is 600 seconds.

Teardown: validate selected ownership/configuration/disks and each snapshot's referenced disks →
graceful shutdown if running → revalidate → `qm destroy --purge 0 --destroy-unreferenced-disks 0`
→ authoritative absence of guest and its captured owned volume IDs. Preserve templates and
unselected resources; never clear native locks, force-stop, or delete arbitrary/unreferenced disks.
Absent teardown targets produce an unchanged absent outcome, after authoritative inventory read.

## Baseline record

Native snapshot description is compact JSON with exactly `schema:1`, `identity`, `config_sha256`.
Hash normalized native configuration excluding only `digest`, `description`, `parent`, and native `vmgenid`;
snapshot normalization also removes `snaptime`, requiring no `vmstate` or `snapstate`.
Validate vmgenid UUID shape when present; rollback deliberately rotates it. SMBIOS UUID remains
bound. The fixture rotates vmgenid on rollback so two cycles prove this native behavior.
Root and EFI are guest-writable snapshot targets; cloud-init CD-ROM is guest-read-only and
excluded by native snapshotting. Its volume reference and generating configuration remain bound.
Validate snapshot row name, positive integer time, absent RAM, metadata shape/identity/hash,
snapshot configuration against current configuration and inventory, and complete disk references.
Native `parent` is permitted only when it names a listed snapshot; snapshot names remain native
validated data, never shell-interpolated. Live guest description remains the guest-v1 marker.
No marker rewrite establishes cleanliness after an incomplete capture. Any failed step leaves
its observed state for inspection; existing guests cannot re-enter fresh capture on retry.
Read-only reruns preserve snapshot time/configuration and guest test data.

## Failure model

- Actors and deployments: cooperative operator/controller; Linux Proxmox x86_64 with supported LVM-thin/ZFS disks; Linux/macOS
  ARM64/x86_64 controllers; all four pinned guest profiles and existing overrides.
- Invariants and assets: selected guest state, immutable baseline, templates/unselected resources.
  Required failures: unsafe selection/confirmation, wrong ownership/inventory, absent/stale/partial
  or RAM baseline, extra/unsupported disks, contention, changed native state, failed commands,
  transport deadlines, and incomplete readiness fail nonzero before the next dependent action.
- Accepted failure classes: privileged out-of-band host edits and unrelated consumers bypass cooperative
  locks; operator exclusive-use attestation is required. No distributed allocation system is added.
- Covered elsewhere: KDIVE owns external services/artifacts; operator owns host infrastructure.
  Recovery: inspect retained partial and native task status, resolve active tasks externally, then
  explicitly selected teardown/recreate when valid; no automatic retry, unlock, stop, or deletion.

## Threat model

- Boundaries added: destructive CLI intent and native snapshot metadata; widened: existing
  controller/native JSON exchange and privileged qm operations.
- Actors: trusted operator/private inventory and authenticated Proxmox host; accidental malformed
  inventory, stale metadata, and concurrent cooperative sessions are not trusted as authorization.
- Controls: exact offline selection/confirmation, native Boolean intent check, existing identity
  and disk validation, bounded typed events, nonblocking locks, strict SSH/TLS, argument-vector
  commands, and sanitized errors/outcomes. Native metadata never becomes a shell command.
- Out of scope: malicious root/compromised trusted endpoints and noncooperating consumers,
  because they already control guest disks; operational exclusivity remains the operator duty.

## Verification and evidence

Focused tests use the existing stateful native boundary fixture, real pipe exchanges where useful,
and CLI validation before mocked remote access. Cover fresh ordering and no RAM, unchanged reruns,
restore with stopped/running guests, read-only re-verification, missing/stale/RAM snapshots,
unsupported disks and extra volumes, wrong ownership, confirmation mismatch, busy locks, command
failure/deadlines, absent teardown and selected-only cleanup. New tests demonstrate red before code.
Existing transport/reboot/guest identity tests remain meaningful contracts and must stay green.
Run make check through precommit and explicit final guardrail; Linux/macOS CI uses that recipe.

After separately approved live target/actions: one distro at a time, exact owned guests, preserve
shared templates/unselected VMs, fresh create/capture, two deliberate file/configuration-change
restore cycles, readiness checks, unchanged rerun, and scoped teardown. Record sanitized real
outcomes/durations and deployed revision. No live proof means no completed issue/merge handshake.
