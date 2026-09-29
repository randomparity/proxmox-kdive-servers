# ADR 0009: Pinned kernel source level

## Status

Accepted (2026-09-28; issue #19 proposal approved by operator).

## Context

KDIVE consumes an unbuilt Linux tree without history. Issue #19 requires a
shallow operator-owned checkout with independently verifiable provenance above
toolchain, without resetting existing trees. Input changes must not silently
invalidate a captured level.

## Decision

Keep repo/ref in vars/kernel-source.json, read only during preparation. Resolve
a pinned 40-hex commit, fetch depth one and detach as the operator. Record
repo/ref/commit in existing level metadata. Offline checks compare the recorded
commit and reject dirty, untracked and ignored artifacts. Refuse mismatching
existing destinations without repair. Use the existing registry and manifest.

## Consequences

Changing inputs affects only future preparation. Capture and replacement remain
operator operations; a failed fetch can leave a tree requiring operator disposition.
Deepening or building in the tree invalidates this level's verification contract.

## Considered & rejected

- Full/blobless history: judgment: unnecessary for the scoped provenance reads;
  explicit issue #19 decision requires shallow source.
- Execute KDIVE's helper: verified: issue #19 places this level before KDIVE
  installation (#20), so that checkout is absent by the dependency contract.
- Git status without ignored entries: verified: KDIVE fetch-kernel-tree.sh
  explicitly permits ignored build products; issue #19 excludes them.
- Resolve remote refs during verification: judgment: captured evidence should
  survive network outages and moving refs without changing its recorded identity.
