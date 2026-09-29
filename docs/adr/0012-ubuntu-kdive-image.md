# ADR0012: Use Ubuntu 26.04 for the installed KDIVE level

## Status

Accepted by operator approval for issue #25 on 2026-09-28.

## Context

KDIVE requires installed Python 3.14 and a matching native guestfs binding. The prior
Ubuntu image was 24.04 with Python 3.12, and its installed proof was deferred.

## Decision

Pin the approved Ubuntu 26.04 release-20260918 image and record its read-only inspected
baseline. Rebuild the level chain in separate operator-assigned replacement resources,
preserving the old guest, template and snapshots. Delegate matching native bindings to
the upstream installer using Ubuntu packages. Resolve one approved current main commit
immediately before KDIVE preparation; retain that pin and the existing equality guard.

## Considered & rejected

- Keep the old image: verified: vars/images.json at ed3a224 records Ubuntu 24.04 and
  Python 3.12.3; issue #25 requires installed Python 3.14/native guestfs proof.
- Add an interpreter/binding build: judgment: duplicates upstream installation ownership
  and expands maintenance beyond the approved image migration.
- Replace resources in place: judgment: preserving the old resources makes this bounded
  proof easier to reverse and follows the operator's explicit replacement approval.

## Consequences

Image-dependent identities and snapshots must be rebuilt; previous captures are not
compatible metadata for this image. The source pin also changes the next preparation
for other supported distributions, while verification uses recorded installed content.
A moving upstream main can still prevent preparation after selection; failure does not
permit advancing the pin. The bounded HTTP proof is not a nested VM or kdump proof.
