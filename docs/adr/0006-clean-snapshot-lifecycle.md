# 0006: Fresh-only clean snapshots and selected reset

## Status

Accepted for implementation following operator interface/metadata approval, 2026-09-28.

## Context

Issue #5 needs repeatable clean guest reset without blessing test-modified state. Existing guest
identity binds source/template and desired configuration; existing controller/native sessions
already keep locks through authenticated guest readiness.

## Decision

Extend that session to capture `clean` only after fresh preparation, verified graceful shutdown,
and before consumer handoff. Capture guest-writable root/EFI disks with no RAM; bind the guest-read-only cloud-init CD-ROM
reference and generating configuration. Snapshot description
holds versioned metadata binding existing guest identity and normalized native configuration.
Boot and verify again before success. Missing/stale baselines on existing guests require explicit
selected teardown/recreation; preserve guest-v1 identity and never adopt older snapshots.

Restore/teardown are read-only plans unless APPLY=1, CONFIRM exactly matches TARGETS, and
EXCLUSIVE=1 attests consumer release. Use existing native locks and completed CLI tasks. Restore
validates baseline before stopping and re-verifies after boot; teardown deletes only inspected
selected owned guests/disks. No new scheduler, inventory schema, or external state store.

## Consequences

Snapshot metadata and source/configuration drift fail closed. Partial tasks remain for inspection;
no automatic cleanup or force-stop follows failure. Teardown can remove a matching complete older
guest without a clean snapshot. Incomplete allocations remain manual recovery. Exclusive-use
attestation cannot fence unrelated privileged operators or consumers; they must cooperate.

## Considered & rejected

- Separate capture command: judgment: permits accidentally blessing used state and duplicates
  the fresh provisioning boundary.
- Automatic replacement/adoption: verified: issue #5 explicitly forbids capturing existing used
  guests and requires explicit recreation for stale/missing baseline.
- External baseline database: judgment: duplicates existing Proxmox resource ownership and
  introduces synchronization/recovery surface without a requested consumer.
