# Guest capacity oversubscription warnings

## Problem and authority

Issue #33: guest admission fails when the selected batch's vCPUs or RAM exceed the
host's observed free capacity, although oversubscription is intended lab practice, and
the reason reaches the operator only as generic text. Scope token: q33-8bbc77cf.
Excluded: storage admission stays a hard failure (issue #33); general host-error
passthrough beyond the capacity warning (separate follow-up candidate); dedicated-core
reservation (none).

Decision: [ADR 0014](../../adr/0014-capacity-oversubscription-warnings.md).

## Design

`guest_host.check_capacity` keeps its metric checks (missing CPU metrics, invalid load,
missing `MemAvailable`) and storage checks as `GuestError` failures. It returns a list of
warning objects instead of raising for demand:

- CPU: `busy = ceil(max(cpu * cpus, load))`; when new cores exceed `cpus - busy`, warn
  `{"resource": "cpu", "requested": <new cores>, "available": max(cpus - busy, 0)}`.
- RAM: when new `memory_mib * 1024` exceeds `MemAvailable` kB, warn
  `{"resource": "memory", "requested": <new MiB>, "available": <MemAvailable kB // 1024>}`.

It returns the list only after storage admission passes, and `[]` for an empty batch.
In both cases `0 <= available < requested`. `admission` prints each warning as one JSON
line `{"phase": "capacity-warning", ...}` before returning, so warnings precede every
per-guest event in plan and apply. The existing verify existence check moves from
`session` into `admission` ahead of capacity, so verify of an absent guest fails without
a warning.

`guests.dispatch` reads leading events through a new `read_admitted(process, host)`:
it consumes `capacity-warning` events, then returns the first other event for the
existing validation. A warning is valid only with exactly the four keys, a resource
not yet seen in this dispatch, and integers (not bool) with `0 <= available <
requested`; anything else raises `ValidationError("Native guest: invalid capacity
warning")`. Because each resource may appear once, at most two warnings are read. A
warning after the first per-guest event fails existing shape validation. A valid
warning prints to stderr:

    Guest capacity warning: node <proxmox_node> batch requests <requested> vCPUs; <available> logical CPUs observed free; guests may contend for CPU
    Guest capacity warning: node <proxmox_node> batch requests <requested> MiB RAM; <available> MiB MemAvailable observed; guests may contend for memory

Every dispatch prints its own warnings, so plan prints once per native host and apply
prints for the pre-apply plan and again for the locked recheck. Stdout is unchanged.
Delivery across native hosts is structural: `main` calls `dispatch` once per host group,
and each dispatch prints before it returns results, so an earlier group's warnings stay
on screen when a later group fails.

The three README capacity paragraphs (quick start, sizing guidance, admission details)
describe warnings and the retained hard failures, and state that the Ansible entry points
hide controller stderr under `no_log`, so warnings are visible only through `make`.

## Failure model

1. **Actors and deployments** — a local operator running `make provision`/`verify` from a
   macOS or Linux controller against the repository's Proxmox hosts over authenticated SSH;
   the host session is this repository's code shipped by the controller. The documented
   Ansible playbooks run the same controller under `no_log`.
2. **Invariants and assets at stake** — storage admission and metric validation still
   block allocation; stdout stays one JSON result per guest; the strict host→controller
   event protocol rejects malformed or unexpected events; through `make`, warnings reach
   the operator for every native host in the selection.
3. **Accepted failure classes** — CPU/RAM contention or host OOM after oversubscription:
   accepted by the issue, the operator controls concurrent load. Observed capacity changing
   between the plan and the locked recheck: bounded, both observations are printed.
   Ansible playbook runs oversubscribe without a visible warning: the role's existing
   `no_log` hides all controller output by design; the README directs operators to the
   `make` plan, and surfacing warnings through the role is a follow-up candidate.
4. **Covered elsewhere** — generic replacement of other host error text: follow-up
   candidate outside this scope. Storage shortfall: existing hard admission check.

## Validation

- `check_capacity` returns exact warning lists for CPU, RAM and both; metric and storage
  failures still raise (`tests/test_guests.py`, `test_capacity_checks_observed_load_and_whole_batch`).
- A host session under oversubscription emits `capacity-warning` before `planned` and
  apply still creates the guest (`TestNativeLifecycle`).
- `main` in plan mode over two host groups prints each node's warning, and the first
  node's warning is printed when the second group fails (`TestController`).
- Verify of an absent guest fails without a capacity warning (`TestNativeLifecycle`).
- `dispatch` prints valid warnings to stderr and returns outcomes; duplicate resource,
  extra key, bool/non-int, `available >= requested`, and unknown resource fail
  (`TestController`, subprocess host stub like
  `test_batch_plan_consumes_back_to_back_events`).
