# Snapshot level contract — proposed design for #17

Status: approved for implementation by operator reply "approve", 2026-09-28.
Base: a71f1e4. Full-spec lane; fixed design denominator 1000 changed lines (L), from
coordinated controller/native/guest protocol, metadata validation and failure fixtures.

## Problem and scope

Extend the current locked native session and authenticated guest RPC to prepare, verify and
restore operator-captured levels. Preserve guest identity, exact legacy clean metadata, clean
verify/restore output, and fresh-only automatic clean capture. No production level is added.
Sources: issue #17 and epic #16; operator approved exclusions on 2026-09-28.

Excluded: production toolchain (#18), kernel-src (#19), kdive (#20); level snapshot
creation/rename/deletion (operator); images/warm caches (epic owner); external consumer
scheduling/fencing/artifacts (operator/consumer).

## Interfaces and ownership

Retain `make verify LEVEL=clean` and `make restore LEVEL=clean`, with clean the default.
`make level LEVEL=<registered-name>` plans by default. Approved mutation gate:
`APPLY=1 CONFIRM=<exact TARGETS> EXCLUSIVE=1`. Preparation installs software and shuts down;
using the existing restore gate makes consumer-release intent explicit. Reject clean, unknown
names, and unsupported operation/LEVEL combinations before remote mutation.

Keep one ordered closed registry in `scripts/guest_verify.py`, which already travels to both
native and guest processes. Each entry owns name, immediate parent, prepare hook and check hook.
No dynamic import, shell hook, user registry or production test-level switch is added. Native
and controller use this registry for chain/name validation; guest RPC executes hooks locally.
The prepare hook returns content; the check hook validates observed content against recorded
content. Recorded content remains authoritative on verify/restore; new configured pins do not
silently invalidate an older snapshot. Later issues define their content contracts.

Keep native snapshot evidence in guest_host, guest software/manifest checks in guest_verify,
and transport/event validation in guests. Extend existing session functions with an explicit
level argument defaulting to clean, leaving the guest identity request unchanged. No separate
state service or lock is introduced. Exact event shapes remain validated; expected selected
level is controller intent, never inferred from a returned event.

## Metadata and chain

Clean remains exactly `{schema,identity,config_sha256}`. Non-clean metadata has exactly
`{schema,level,parent,parent_identity,identity,config_sha256,content}` with schema integer 1.
Parent identity is template_host.digest of the complete validated parent metadata.
Validate every registered ancestor from clean: positive snapshot time; no RAM or incomplete
state; complete exact schema; guest identity and normalized configuration hash; description
matching native snapshot config; normalized snapshot/current config equality; declared parent
matching both registry and Proxmox tree. Reject missing intermediate levels and wrong links.
Do not trust current guest disk contents during restore admission.

For each non-clean ancestor, guest checks compare the root-owned 0644 regular manifest at
`/var/lib/kdive-levels/<name>.json` with validated snapshot metadata and run its check hook.
No clean manifest is introduced. Write new manifests atomically without following symlinks;
reject existing unsafe directory/file ownership, mode or type. Bound RPC and JSON sizes to
existing transport limits, reject duplicate JSON keys and unexpected fields, and keep errors
public-safe. Compare parsed canonical data rather than cosmetic JSON whitespace.

## Execution and failure behavior

Verify requires a running guest, validates native chain then existing baseline guest readiness,
checks every level manifest/hook, and reports selected snapshot evidence.

Restore plan and apply accept a stopped or disk-modified guest. Validate native chain before
shutdown, repeat under existing locks and before rollback, roll back selected level without
RAM, boot, then perform readiness and all chain guest checks. Do not require guest manifests
before rollback: repairing disk state is the purpose of restore.

ZFS admission rejects a rollback target with newer snapshots/descendants before shutdown,
including restoring clean after a child capture. Report operator inspection/removal of newer
snapshots; never delete them. Validate actual storage type and reject ambiguous inventory.
Repeat admission immediately before rollback; external privileged writers remain outside locks.

Preparation requires existing owned running guest, a verified parent chain, current native
parent equal to that parent, and no snapshot already bearing the target name. Run ordinary
readiness and parent guest checks, prepare the new level, assemble/write its manifest, run its
check and parent checks, then gracefully shut down with forceStop=0. Revalidate stopped config
and parent/snapshot inventory before emitting READY and exact canonical single-line description.
No snapshot command is called. A failed hook, malformed response, lost acknowledgement, timeout
or failed shutdown leaves inspectable state and emits no READY. Do not retry mutations.

The operator captures the no-RAM snapshot and starts the guest before verify. Preparation does
not claim the disk is an untouched byte-for-byte parent: current parent provenance plus checks
establish the specified contract. Unrelated disk content is not measured.

## Failure model and validation

Automated fixtures cover exact schema/type/link/config/manifest mismatch, missing ancestor,
RAM/incomplete state, wrong Proxmox parent, ZFS refusal before any mutation, stopped/dirty
restore, repeated restore, unknown names, apply gates, unsafe manifest paths, and no READY on
prepare/check/write/shutdown/config-race failures. Assert operation order and absence of level
snapshot creation/deletion. Controlled wrong-parent and premature-READY faults must turn their
focused tests red, then be removed. Existing clean tests remain unchanged where possible.
Run focused snapshot/controller/guest tests and `make check`; CI Linux/macOS checks separately.
No full suite is needed to review this proposal alone.

## Approved live proof and operator boundary

Read-only native inspection confirms the selected Ubuntu guest is stopped, current parent clean,
and only a no-RAM clean snapshot exists. Its backing storage is ZFS. The earlier supplied
snapshot listing belongs to Fedora. Private inventory resolves both aliases; identifiers stay
out of public artifacts. Native metadata inspection is not full guest verification or proof of
exclusive availability.

Use a test-only fixture registry entry `contract-test` with parent clean, writing/checking a
fixed marker and its manifest. A focused live harness injects only that fixture into the same
transported source; no production selector or installed level is added. Record final source
HEAD plus fixture hash so proof distinguishes tested production code from test instrumentation.

Operator approved exclusive-use/live mutation authorization for Ubuntu: start it; verify clean;
prepare fixture and observe stopped state plus READY; pause for operator capture with emitted
name/description; start and verify contract-test; modify only the fixture marker; restore
contract-test and verify marker repair. Probe clean restore plan refusal while child exists.
Finish with the guest stopped. Operator then removes contract-test before subsequent clean
rollback or production preparation; code never creates/deletes that snapshot. No all-guest run.

The operator approved the prepare gate and bounded fixture/live mutation method; human
capture/deletion remain execution checkpoints. No other guest mutations are authorized.
