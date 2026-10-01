# ADR 0016: Optional level snapshot capture

## Status

Accepted. Amends ADR 0007's capture rule and rejection of tool-side higher-level
capture; its other rules and ADR 0013's storage rule remain unchanged.

## Context

Issue #47 requests an explicit alternative to copying prepared metadata into a
native snapshot command and separately booting and verifying each prepared level.
The operator approved `CAPTURE=1` on `make level`, backed by `--capture`.

## Decision

Capture is off by default. With capture requested, level preparation may create
its selected snapshot only with the existing APPLY, exact CONFIRM, and EXCLUSIVE
gates. Planning remains read-only. The host retains the existing lifecycle locks
through preparation, stopped-state checks, capture, boot, and guest verification.
Reuse the clean capture command construction: native `qm snapshot`, `--vmstate 0`,
and the prepared metadata as compact JSON. Reject an existing target; read back
its metadata and native configuration before boot. Report success only after the
selected level verifies. Manual preparation keeps its existing output and stop.

## Consequences

Failures preserve the stopped or running guest and any partial snapshot for
operator inspection. Capture is attempted once: no rename, deletion, replacement,
rollback-on-error, or automatic retry. Exclusive use remains an operator assertion;
cooperating tools share locks, but an unrelated privileged writer is not fenced.
Clean provisioning, metadata schemas, and level definitions are unchanged.

## Considered & rejected

- **Manual capture only.** verified: #47 explicitly requests optional tool-side
  capture, replacing #16/#17's operator-only assignment that grounded ADR 0007.
- **Capture by default.** judgment: changes the established preparation contract
  and removes the operator's chance to inspect before capture.
- **Separate capture command.** judgment: splits one authorized prepare/capture
  lifecycle and requires another admission protocol for prepared metadata.
