# 0017: Compose clean-host installation proofs around the upstream runner

## Status

Accepted, 2026-10-02, for issue #30.

## Context

KDIVE PR #3071 provides a controller-side SSH runner at
`1321c285d02aadfde6e5842710521aaa0c2f6881`. This repository owns the verified
Proxmox clean snapshot and must bind it to proof evidence and restore it afterward.

## Decision

Add an opt-in, single-guest orchestration target. Keep the existing lifecycle locks
through first restore, upstream proof and final restore. Refuse snapshots above clean.
Use the pinned controller checkout and its environment; upstream transfers source.
Keep upstream evidence unchanged and bind verified snapshot evidence in a private
wrapper record. Forward an optional operator-owned prerequisite script without
providing one. Preserve proof status and report reset failure independently.

## Consequences

The target needs a clean pinned KDIVE checkout, its dependencies, a kernel bundle
and an exclusive clean-only guest. Warm fixtures are refused. Catchable interruption
attempts reset; power loss or lost transport cannot promise verified recovery.

## Considered & rejected

- Two independent restore commands: judgment: releases cooperative locks during proof.
- Guest-side runner and baseline argument: verified: inspected merged PR #3071 runner
  `main`/`run`; it runs controller-side and accepts no snapshot identity argument.
- Duplicate installation checks or automatic prerequisites: judgment: crosses upstream
  ownership and could turn a missing prerequisite into an unrecorded prepared baseline.
