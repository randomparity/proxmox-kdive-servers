# Optional level snapshot capture — issue #47

## Scope and authority

Implement #47 and the operator-approved CAPTURE=1 / --capture interface under
[ADR 0016](../../adr/0016-optional-level-snapshot-capture.md). No ownership move:
the host keeps native mutation and locking, the controller keeps guest checks.
Preserve manual behavior, clean semantics, level definitions and metadata schema.
Excluded owners: operator owns rename/delete/replace, rollback/retries, whole-chain
scheduling and inventory/storage migration; #41 owns live restore proof; existing
contract owners own schema/clean/level changes. No live mutations in this worker.

## Success

1. Default preparation still stops and prints READY TO SNAPSHOT plus metadata.
2. Capture on level uses APPLY, exact CONFIRM and EXCLUSIVE; plan never mutates.
   Capture on another operation is rejected before remote access.
3. Capture follows verified preparation and stopped-state/admission checks, creates
   only the selected absent target with vmstate0 and exact compact prepared JSON.
4. Native row/config/metadata readback completes before boot. Boot and selected
   level verification complete before a captured success result is returned.
5. A failed preparation/capture/readback/start/check returns failure and retains
   inspectable state; no automatic capture retry or cleanup.

## Architecture and protocol

Export CAPTURE and parse --capture like APPLY. Carry a strict boolean capture in
controller-host envelope, permitted only for level/plan-level. No schema migration
is needed because the controller bundles the native helper in each SSH session.
Keep the prepare exchange. In capture mode its next event is level-boot after
successful capture/readback/start, instead of terminal snapshot-ready. Include the
same level and prepared metadata plus guest UUID. Controller validates exact
metadata against its preparation result, verifies the new boot against the prior
boot ID, acknowledges level-boot, then uses the existing levels verify exchange.
The host checks acknowledgement and native readiness before that exchange and
emits ready only after it completes. Controller validates final evidence against
the selected level; capture results use action captured and normal evidence output.
Manual mode remains snapshot-ready and its existing two-line operator output.

Extract the existing native qm snapshot command construction into one function
used by clean and level capture. Existing baseline/chain validators still validate
native rows, vmstate0, parent, config and content; additionally require the target
row's description to equal prepared compact JSON before boot, and require the
post-boot verify chain to contain that same prepared metadata. Hold current locks
throughout, and perform pre-capture stopped inspection/admission again.

## Failure model

- Prevent: invalid opt-in/gates, existing target, changed parent/config/metadata,
  RAM snapshots and malformed/out-of-order host events, using existing validation.
- Detect and retain: native command failure or timeout, invalid readback, boot or
  verification failure; no success result and no deletion/retry/rollback.
- Accept: unrelated privileged writes between checks are not fenced by advisory
  locks; EXCLUSIVE remains operator-owned. Partial commands may have succeeded
  despite transport loss, so inspect before another operator invocation.
- Deployment: supported controller Linux/macOS ARM64/x86_64 and x86_64 Proxmox
  guests using supported snapshot storage; offline fake-native tests do not prove
  real hardware readiness. Live proof belongs to separately authorized #41.

## Validation

Use existing native fixtures and controller subprocess exchange tests to cover
opt-in/default, exact gate refusals, compact metadata, duplicate/racing snapshots,
stopped validation, readback drift/RAM/config failure before boot, post-boot check
failure, success ordering and clean regression. Run focused tests red then green,
Ruff during iteration, and make check via the commit hook for assembled candidate.
