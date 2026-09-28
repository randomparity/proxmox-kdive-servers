# ADR 0008: Toolchain snapshot provenance

## Status

Accepted for implementation under issue #18 and the operator's 2026-09-28 approval.

## Context

The toolchain speed layer must remain usable when later levels install additional
KDIVE dependencies. Existing level hooks verify ancestors after preparation.

## Decision

Extend the existing guest hooks with distro package lists and common operator-login
checks. Record complete installed-package hash as capture provenance; enforce required
packages, capabilities and recorded uv/just/docker/libvirt versions on verification.
Use distro sources except the explicitly approved signed Docker stable RHEL source
on Rocky. Install pinned operator uv/just without running their installer as root.

## Consequences

Additional packages do not invalidate ancestors. Critical tool upgrades do, requiring
operator-controlled re-preparation. Package gaps remain visible instead of silently
adding repositories. This layer does not replace KDIVE's installation contract.

## Considered & rejected

- **Require the whole package set unchanged.** verified: `scripts/guest_verify.py`
  `run_level` rechecks parent hooks after descendant preparation (merged PR #21);
  epic #16 requires the KDIVE descendant to install additional dependencies.
- **Run all KDIVE setup here.** judgment: duplicates the next level's ownership and
  couples the reusable toolchain snapshot to one evolving KDIVE revision.
- **Leave prerequisites unprepared.** judgment: retains the repeated preparation
  cost issue #18 explicitly requests removing.
