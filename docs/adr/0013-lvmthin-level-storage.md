# 0013: Use LVM-thin storage for level-bearing guests

## Status

Accepted by operator decision for issue #32, 2026-09-29. Amends the ZFS consequence of
ADR 0007; ADR 0007 is unchanged.

## Context

ADR 0007 records that on ZFS, newer snapshots prevent rollback to a lower level until the
operator removes them. A guest carrying `clean`, `toolchain`, `kernel-src` and `kdive` on
`zfspool` can therefore roll back only to its newest level, and operators who need to move
between levels in both directions must destroy every newer snapshot first.

On 2026-09-29 the operator rebuilt the KDIVE guest storage from a single-disk `zfspool` to
`lvmthin` for this reason. A live probe on the new thin pool observed Proxmox rolling back
from snapshot `second` to `first` and forward again, keeping the other snapshot each time,
with disk contents matching. The Proxmox `LvmThinPlugin.pm` behavior the issue cites is
consistent with the probe: `volume_snapshot_rollback` removes only the current volume and
recreates it from the chosen snapshot, and `volume_rollback_is_possible` returns 1
unconditionally. This ADR takes both as the issue's observed evidence.

## Decision

Use `lvmthin` storage for guests that carry snapshot levels when the operator needs free
movement between levels. `zfspool` remains an admitted storage type, and its rule stands:
rollback is possible only to the newest snapshot, and restore rejects a lower rollback
before any mutation. This amends ADR 0007's consequence "On ZFS, newer snapshots prevent
lower rollback until the operator removes them" from a standing property of levels to a
property of `zfspool` alone. No code changes: `rollback_admission` already applies the
newer-snapshot block only to `zfspool`.

## Consequences

On `lvmthin`, restore may target any level and later return to a higher one; no operator
snapshot removal is needed. `lvmthin` provides no data checksums or compression. Thin-pool
data or metadata exhaustion pauses guests, so the operator must monitor pool usage. The
README storage section carries this guidance, and the ZFS-specific measurements in the level
proofs are labelled as historical ZFS measurements, not rewritten.

The tool's `lvmthin` restore path has so far been exercised only by fake-backed tests. A live
`make restore` proof (lower level, then back up) awaits rebuilt guests and is tracked outside
this change.

## Considered & rejected

- Keep `zfspool` and document the limit only: verified: ADR 0007 and the README already state
  the limit, and it forces snapshot destruction to reach any lower level.
- Edit ADR 0007 in place: judgment: ADRs are immutable records; this ADR amends it instead.
- Block or warn on `zfspool` at admission: judgment: the newest-only rule is already enforced
  by `rollback_admission`; the gap is guidance, not behavior.
