# 0007: Operator-captured snapshot levels

## Status

Accepted for implementation following operator scope and interface approval, 2026-09-28.

## Context

Issues #16 and #17 extend bare clean guests with named prepared states. The operator explicitly
owns capture. ADR 0006 protects clean from adoption of used guests; that contract stays intact.

## Decision

Extend the locked native lifecycle and authenticated guest RPC with a closed ordered level
registry. Prepare verifies the parent, mutates only under APPLY/CONFIRM/EXCLUSIVE, writes and
checks an in-guest manifest, shuts down gracefully, then emits snapshot name and description.
The operator captures the level. The tool never creates, renames or deletes a level snapshot.

Keep clean metadata unchanged. Each higher snapshot binds its native configuration, identity,
parent name and parent metadata digest, and content. Verify compares the description with its
root-owned guest manifest and runs every ancestor hook. Restore admits the native chain before
stopping, rolls back, boots and checks guest content. Reject ZFS rollback with newer snapshots
before any mutation. Preserve fail-closed metadata and partial-state handling.

This amends epic #1's installation/cache non-goals only for explicitly requested prepared
levels and ADR 0006's capture-path rejection only for operator-captured higher levels. This
change adds no production level, image/cache preparation, scheduler or automatic cleanup.

## Consequences

Operators must capture exact no-RAM metadata and start guests before read-only verify. On ZFS,
newer snapshots prevent lower rollback until the operator removes them. Parent checks cannot
prove an unmeasured disk is pristine; they establish only recorded provenance and hook contracts.
External privileged writers must respect exclusive use because tool locks cannot fence them.
Interrupted preparation stays inspectable and emits no READY; it is not automatically retried.

## Considered & rejected

- Keep clean-only behavior: verified: #17 requires named verify/restore and operator preparation.
- Tool captures higher snapshots: verified: epic #16 assigns capture solely to the operator.
- Configurable plugins or a metadata service: judgment: adds loading/state-management surfaces
  to an ordered list owned in this repository.
- Require guest verification before rollback: verified: tests/test_snapshots.py at a71f1e4
  explicitly tests restore of modified disks and a stopped guest; this would prevent recovery.
