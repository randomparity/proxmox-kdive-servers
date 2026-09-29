# ADR 0015: Carry allow-listed host failure reason codes

## Status

Accepted by the operator's approval of issue #40 scope on 2026-09-29.

Extends [ADR 0014](0014-capacity-oversubscription-warnings.md), whose consequence "Other host
error text is still not passed through" still holds; ADR 0014 is otherwise unchanged.

## Context

The guest host helper reports a failure as `{"error": "<text>"}` and the controller replaces
every such event with one generic message, so an operator whose batch failed because a vmid
was taken, a template was missing or storage ran out learns only to "inspect ownership,
configuration and prerequisites". The host text is deliberately not echoed: the controller
treats host output as untrusted and prints only text it owns.

## Decision

A small set of host refusals carry a reason code: the host raises them through
`refuse(condition, code, message)` in `scripts/guest_host.py`, and `main()` adds
`"code": <code>` to the error event only for those refusals. The controller owns a fixed
`HOST_REASONS` map in `scripts/guests.py` from code to message text. `read_event` uses the
mapped text only when the error event has exactly the keys `error` and `code` and `code` is
a string key of that map; every other error event keeps the generic message. The host's
`error` text is never printed by the controller.

The codes cover operator-actionable causes: a taken guest ID, an absent template,
disabled host nesting, missing required guests, foreign ownership, insufficient storage and
a root disk smaller than the template. Other refusals stay uncoded until an issue asks.

## Consequences

The operator sees a specific, actionable reason for the coded refusals; all other host
failures read exactly as before. Controller and host ship together, so an unknown code is a
defect, not a version skew, and it degrades to the generic message rather than failing
differently. A test ties the codes the host can raise to the controller's map. Adding a
reason means a host call site, a map entry and its text in one change. The template helper's
error protocol is unchanged.

## Considered & rejected

- **Pass host error text through.** judgment: widens the host-to-controller text channel
  ADR 0014 keeps closed; the controller would print strings it did not write.
- **Controller maps host error text to reasons.** judgment: couples the controller to
  host prose and makes every wording edit a silent protocol change.
- **Code every host refusal.** verified: `rg -c "check\(" scripts/guest_host.py` reports
  103 lines at commit ce576a6. judgment: most name internal drift the operator cannot act
  on without inspecting the host anyway; issue #40's exclusions defer this to demand.
- **Do nothing.** judgment: issue #40 reports that the generic message hides causes the
  operator can fix directly.
