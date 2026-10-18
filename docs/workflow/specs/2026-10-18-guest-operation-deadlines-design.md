# Controller verify/restore deadline contract

Accepted for bounded implementation by the operator, 2026-10-18; [ADR 0018](
../../adr/0018-guest-operation-deadlines.md). Source baseline: baf46d90.

## Scope and success

C1: bound the direct Python verify/restore CLI, including read-only restore plans, admission,
selected guest groups, transport, output and local cleanup. C2: preserve ownership, snapshots,
authorization and 65536-byte combined guest output cap. C3: expiry is failure with remote state
unverified. C4: imported install-proof helpers and other operations retain their existing behavior.
No remote cancellation, automatic retry, snapshot mutation policy or host release is added.

## Operator interface

Supply `--operation-timeout-seconds INTEGER` in 21..86400. `make verify`/`make restore` forward
`OPERATION_TIMEOUT_SECONDS` as one argument; the Ansible guest role forwards that same environment
value only for verify/restore. Missing/invalid values fail before API/SSH access. Other operations
reject the option. There is no default duration. An illustrative value 7200 is not a live approval.

At start S on the controller monotonic clock, total T defines work cutoff W=S+T-20 and exit cutoff
E=S+T. The 20 seconds are a shared local cleanup reserve, not per guest/child. Existing method
limits are capped by remaining work time. Do not reset T between preflight and apply groups.
One main-thread owner requires unused, unblocked native SIGALRM/ITIMER_REAL. It stays armed until
process completion with SIG_DFL, rather than a potentially deferred Python callback.

API admission uses that owner to arm min(E,W,API-start+60), retaining its 30-second HTTP cap,
1MiB response bound and TLS/credential validation. It rearms only remaining E after returning.
Other template/direct callers retain their existing 60-second timer behavior.

## Failure and cleanup

Stop new work at cooperative exhaustion. Close raw pipes; signal only locally owned child
transports. Share 5-second TERM and 5-second KILL/reap intervals across the child set, capped by E.
Cleanup failure remains failure. Native alarm expiry can skip cleanup and leaves uncertainty.
Normal CLI completion explicitly verifies output/reap, then uses process-only exit without
implicit Python finalization waits. Imported library calls do not use this process exit.

Cooperative timeout exits 124, ordinary operation failure 1; native SIGALRM is signal termination
(Popen reports negative signal; shells commonly report 128+signal). Make may report 2. Partial
records do not override the final status. No timeout proves remote completion or authorizes
rollback/retry/release. Retain ownership and independently observe native tasks and state.
A Python-CLI bound is not a Make/shell or raw native-command bound.

## Failure model and trust boundaries

Actors: operators on Python>=3.12 Linux/macOS ARM64/x86_64 controllers; guests remain x86_64.
Assets: source/snapshot identity, strict transport, locks, honest failure and bounded output.
Accepted classes: kernel nontermination/unscheduled or failed controller outside responsive-POSIX
application guarantees (ADR 0005); remote task uncertainty after local expiry retains HOLD.
Other owners: install-proof orchestration (ADR 0017), independent preservation/task evidence and
separately authorized maintenance/certificate windows. No platform runtime arm is implied.
Untrusted remote responses keep existing validation and sanitized diagnostics. Duration is
validated as decimal before access; signals belong only to the exact local child Popen objects.
No credential, remote command, privilege or dependency is added. Privileged concurrent host
mutation and hostile controller runtimes are outside this contract.

## Verification

Focused tests use actual local fake transports and the actual API wrapper with HTTP faked:
invalid inputs/timer masks; shared invocation state; stalled writes, partial events, output EOF
with live child, API stalls/success and blocked output; cleanup/reap, native C-stall expiry,
finalization and interrupt failures; unchanged imported helpers; actual Make/Ansible argv.
Controlled omissions must fail, then restored tests pass. Run controller offline guards; report
Linux/macOS runtime arms separately. These proofs do not require or authorize managed hosts.
