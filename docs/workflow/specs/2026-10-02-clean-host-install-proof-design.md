# Clean-host installation proof orchestration

## Scope and interface

Issue #30 composes the merged KDIVE #2807 runner; ADR 0017 records the boundary.
One explicit inventory alias is required. `make kdive-install-proof` takes
`KDIVE_CHECKOUT`, `KERNEL_BUNDLE`, `GUEST_IMAGE`, and a new private `OUTPUT`
under ignored `reports/`. Optional `OPERATOR_PREREQUISITES` is forwarded unchanged
with a recorded digest; it is operator-owned upstream input, not wrapper installation.
Plan is read-only. Apply requires `APPLY=1 CONFIRM=<exact TARGETS> EXCLUSIVE=1`.
The selected guest must have only `clean` (plus native `current`) snapshots.
Supported families map Ubuntu/Fedora/Rocky to debian/fedora/enterprise; openSUSE is refused.
The controller checkout must be clean at the immutable repository pin, with its own
Python 3.14 environment able to import the runner. The wrapper supplies no dependencies.

## Sequence and evidence

Reuse authenticated inventory admission, strict SSH pins and guest verification.
A native proof session holds the existing global/template/guest cooperative locks
across two restores and the controller-side runner. The native host refuses higher
snapshots again under lock, restores clean, and completes the existing controller
verification handshake. Only verified baseline evidence permits starting the runner.
The upstream runner transfers its exact candidate source and owns its proof/evidence.
Private SSH/SCP launchers apply the inventory identity and strict isolated SSH config;
no ambient SSH configuration or shell interpolation selects a target. Explicit inventory
keys use IdentitiesOnly; otherwise the existing transport authentication defaults apply.

The native session enters a final restore in `finally` after entering the first
restore, including a failed first verification, rejected proof acknowledgement or
controller disconnect. A reset-phase marker lets the controller verify the final boot.
The controller terminates the runner process group on SIGINT/SIGTERM/timeout, then
requests and verifies reset. Signals received during reset are deferred until it finishes.
Controller whole-run timeout is 24 hours; bounded termination gets 30 seconds. Native
proof acknowledgement waits 24 hours plus 120 seconds, beyond termination/handoff.
A reset marker forbids starting or continuing proof. The wait cannot hold locks forever.
A lost connection may permit native rollback/boot but cannot establish controller
verification; report reset unverified, retain state and require operator inspection.

Keep upstream output unchanged in a fresh mode-0700 run directory, with private logs.
Write wrapper provenance separately: candidate SHA, controller revision, verified
initial/final clean snapshot evidence, operator-script digest, runner return code,
reset status and SHA-256 digests of retained upstream evidence. Do not put addresses,
keys or raw subprocess errors in public stdout. Result JSON and logs remain private.
Preserve runner exit codes 0/1/2/3 and catchable signal status; when reset fails,
report it separately and use a nonzero wrapper exit if the runner succeeded or never ran.

## Failure model

- Actors and deployments: trusted local operator on Linux/macOS controllers with exclusive
  use of an owned x86_64 guest; cooperative repository lifecycle clients share host locks.
- Invariants and assets: warm snapshots remain untouched; correct owned guest only; strict
  SSH identity; verified clean baseline; truthful proof/reset outcomes; private evidence.
- Accepted failure classes: SIGKILL, controller/host power loss and permanent network loss
  cannot guarantee a verified reset; native best-effort reset and retained evidence require
  operator recovery. External privileged writers can bypass cooperative locks.
- Covered elsewhere: KDIVE runner owns proof semantics/prerequisites/evidence schema;
  existing lifecycle admission owns snapshot/config validation; native POWER is KDIVE #2818.

## Threat model

- Added boundaries: CLI paths to controller checkout/execution, operator script to upstream
  runner, private SSH launchers to SSH/SCP, runner output to wrapper provenance.
- Widened boundaries: native session waits across external proof and accepts completion;
  existing inventory, TLS, SSH pin and event validation remain authoritative.
- Actors: trust the operator, pinned source, private inventory and authenticated native host;
  reject malformed events, changed checkout, unexpected snapshots and hostile path shapes.
- Controls: explicit mutation gates, exact pin/clean checks, closed argument lists, strict
  private SSH config, private fresh ignored output, file-type checks and streamed hashing,
  bounded runner/session timeouts, finally reset with separate status. Logs stay private.
- Out of scope: compromised controller/root or upstream implementation, privileged external
  mutations and hardware failures; these are outside the cooperative trust model.

## Validation

Behavior tests mock only SSH/process boundaries. Cover gates/one alias, unsupported family,
wrong source, higher snapshots, lock lifetime, both verified restores, upstream argv/input
binding, proof exits, first-verification failure, interruption, reset failure and privacy.
Controlled faults must turn new critical cleanup/guard tests red before restoring code.
Focused tests and lint precede commits; mandatory precommit runs `make check`, as CI does.
Live proof uses an explicitly authorized dedicated clean-only guest and existing bundle;
report both reset arms and actual upstream result without equating wrapper success with
installation success. The four warm fixtures are not eligible targets.
