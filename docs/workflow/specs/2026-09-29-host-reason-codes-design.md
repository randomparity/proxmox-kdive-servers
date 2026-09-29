# Host failure reason codes

## Problem and authority

Issue #40: every host-side guest `error` event becomes one generic controller message, so
operator-fixable causes never reach the operator. Scope token: q40-a06db9f8. Excluded
(operator-approved 2026-09-29): coding all free-text sites (future issue on demand); echoing
host text (ADR 0014); Ansible `no_log` visibility (#39); the template helper protocol.

Decision: [ADR 0015](../../adr/0015-host-failure-reason-codes.md).

## Design

Host (`scripts/guest_host.py`):

- `class ReasonError(GuestError)` stores `code`; `refuse(condition, code, message)` raises
  it when `condition` is falsy. It is a `GuestError`, so existing handlers are unaffected.
- `main()` emits `{"error": str(error), "code": error.code}` for a `ReasonError` and the
  existing `{"error": str(error)}` for every other failure. Host message text is unchanged.
- These existing checks become `refuse` calls with the same condition and message:

| Code | Sites |
|---|---|
| `vmid-in-use` | `resource_present` foreign resource (every caller, including post-teardown); `clone` ID became occupied |
| `template-absent` | `admission` template absent |
| `nesting-disabled` | `admission` virtualization exposure absent; nesting disabled |
| `guests-missing` | `admission` level, restore and verify require existing guests |
| `ownership-differs` | `inspect_removable` ownership differs |
| `storage-insufficient` | `check_capacity` storage shortfall |
| `disk-too-small` | `admission` root disk cannot shrink the source allocation |

Controller (`scripts/guests.py`): `HOST_REASONS` maps each code above to controller text.
`read_event` raises `ValidationError("Native guest: " + text)` where `text` is
`HOST_REASONS[event["code"]]` when the event is a dict whose key set is exactly
`{"error", "code"}` and whose `code` is a `str` key of `HOST_REASONS`; otherwise `text` is
the existing generic "operation failed; inspect ownership, configuration and prerequisites".
`main()` already prints it as `Guest operation failed: <message>`.

## Failure model

1. Actors and deployments: a local operator running `make` or the playbooks on the
   controller; the guest host helper over SSH on one Proxmox node.
2. Invariants and assets: the controller prints only text it owns (ADR 0014); stdout stays
   one JSON result per guest; a failed host operation still fails the run with exit 1.
3. Accepted failure classes: an uncoded refusal shows the generic message (bounded,
   excluded by scope); a code emitted by a mis-edited host but missing from the map shows the
   generic message (the structural test below makes that drift fail CI).
4. Covered elsewhere: template helper errors (unchanged protocol); `no_log` hiding stderr
   in playbooks (#39).

### Threat model

- Boundary: host stdout → controller `read_event` (existing, widened by one optional field).
- Actor: the host helper runs as root on an operator-owned node; its output is treated as
  untrusted data, not as a compromised adversary.
- Control: exact key set, `type(code) is str`, membership in a fixed map; the value only
  selects controller text and is never printed, formatted or used as a path.
- Out of scope: a compromised host can already fail or fake any result; choosing a wrong
  code only changes which fixed message prints.

## Success and validation

- Each code maps to its own text; an unknown, non-string, `None` or missing code, or an extra
  key, prints the generic message; host `error` text never appears (`tests/test_guests.py`).
- `guest_host.main` emits `code` only for a `ReasonError` (`tests/test_guests.py`).
- Every literal code passed to `refuse` in `scripts/guest_host.py` is a `HOST_REASONS` key and
  every key is used (AST test in `tests/test_guests.py`).
- Representative sites raise the expected code: storage shortfall, foreign vmid, missing
  guest for verify (`tests/test_guests.py`).
- `make check` passes.
