# 0018: Bound verify and restore controller lifetimes

## Status

Accepted by the operator for bounded implementation, 2026-10-18.

## Context

Per-RPC/event timers and implicit subprocess cleanup do not bound a controller invocation.
API admission also owns its own timer. A local transport exit does not prove native completion.

## Decision

Require an explicit 21..86400-second invocation budget for verify/restore, including plans.
One monotonic owner includes admission, selected groups, output and local cleanup; reserve 20
seconds for cleanup. Keep native SIGALRM termination armed through process completion and
coordinate API admission's 60-second cap through that owner. Reuse existing selector transport.
Finish CLI output/reap explicitly before process-only exit; imported helper callers retain their
existing behavior. Timeout retains remote uncertainty, ownership and independent proof duties.

## Consequences

Operators must supply the timeout; no default maintenance duration is inferred. Hard expiry
may leave local or remote work incomplete. Cooperative expiry 124, hard signal termination and
Make failure 2 are distinct. Raw native commands and outer wrappers gain no completion bound.
The kernel/scheduler failure boundary in ADR 0005 remains; install-proof retains ADR 0017 ownership.

## Considered & rejected

- Larger independent timeouts — verified: guests.py at baf46d90 has sequential 1800/2400-second
  waits plus blocking writes and implicit Popen exit waits, without one invocation clock.
- A Python callback as final backstop — verified: Python 3.12 signal documentation states that
  a C-only computation may delay Python handlers; native SIG_DFL termination avoids that callback.
- External timeout alone — judgment: does not coordinate admission/cleanup or establish remote
  task completion. A new remote cancellation protocol would exceed this local controller scope.
