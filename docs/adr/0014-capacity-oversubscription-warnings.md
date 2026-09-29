# ADR 0014: Warn on guest CPU and RAM oversubscription

## Status

Accepted by the operator's approval of issue #33 scope on 2026-09-29.

Amends the capacity admission consequence of
[ADR 0003](0003-verified-guest-lifecycle.md); ADR 0003 is otherwise unchanged.

## Context

Guest admission required new vCPUs plus observed busy CPUs to fit the host's logical
CPUs, and new RAM to fit `MemAvailable`. Lab guests are not loaded at the same time, so
oversubscribing CPU and RAM is intended practice; the hard check refused the default
four-guest batch on a 24-CPU host. The host's reason also never reached the operator:
the controller replaces every host error with generic text.

## Decision

CPU and RAM demand above observed free capacity is a warning, not an admission failure.
Missing or invalid CPU, load or `MemAvailable` metrics and storage shortfall stay
failures. The host reports each oversubscribed resource as one structured event —
`{"phase": "capacity-warning", "resource": "cpu" | "memory", "requested": <int>,
"available": <int>}` — after admission succeeds and before the first per-guest event. The
controller validates the event strictly and prints a fixed-format warning to stderr,
built from its own inventory node name and the validated integers. Every dispatch prints
its own warnings, so an apply shows the unlocked pre-apply observation and the locked
recheck, per native host.

## Consequences

Oversubscribed guests can contend for CPU and RAM; the operator controls concurrent load.
Host swap or OOM under full load is possible and is not detected by admission. Stdout
remains one JSON result per guest. Controller and host code ship together, so the new
event needs no version negotiation. Other host error text is still not passed through.
The Ansible playbooks hide controller stderr under `no_log`, so an oversubscribed
playbook run succeeds without a visible warning; `make provision` shows it.

## Considered & rejected

- **Keep hard admission.** judgment: blocks the documented default batch for a workload
  that is never fully concurrent.
- **Pass host warning text through to the operator.** judgment: widens the host-to-
  controller text channel the controller deliberately does not echo; structured integers
  carry everything the warning needs.
- **Add a `warnings` field to every result event.** judgment: repeats one batch-level
  observation per guest and changes every event shape the controller validates.
- **Operator-set oversubscription ratio.** judgment: new configuration nobody requested;
  a warning leaves the decision with the operator.
