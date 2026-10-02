# KDIVE standard system command lookup

## Problem and scope

Issue #41's authorized Rocky preparation reaches the pinned installer's dependency
check, but `kdive_command` removes the system sbin directories from its controlled
PATH. Rocky installs `ss` and `tcpdump` in `/usr/sbin`; both packages are present,
but the installer reports them missing. This blocks the retained-snapshot proof.
The operator approved exact source commit `b6dfb269cc4b4f8cf27447e3621cb7cde1a32e7b`,
selected-guest rebuilds with two keys, and exclusive Rocky preparation and restores.
The shared command repair is necessary to finish that approved preparation.

## Design and success

Keep `kdive_command(request, phase, script, timeout=120)` as the sole owner of the
KDIVE command environment. Append `/usr/local/sbin:/usr/sbin:/sbin` to its existing
operator-local and standard bin PATH. Preserve the existing directory order,
`env -i`, shell flags, timeouts, output bounds and private failure logs.
No caller migration or new interface is required. This implements the existing
installer-delegation contract in ADR0010; ADR0013/0016 retain restore/capture policy.
A per-command workaround would duplicate lookup policy; inherited PATH would
weaken isolation. No new architecture decision is introduced.

Success requires a regression that fails for missing trusted sbin lookup and
passes after this repair, while excluding inherited and relative PATH entries.
The README's equivalent operator command uses the same directory list.
Live completion requires committed controller source matching transmitted helpers,
successful Rocky kdive capture, reviewed clean and kdive restore plans, each
restore followed by independent verification, both snapshots retained without RAM,
and a healthy final kdive level. Dated public evidence records versions, outcomes,
the stock Python 3.14 prerequisite, and unexercised arms without private identifiers.

## Failure model

- Actors and deployments: trusted operators on Linux/macOS ARM64/x86_64 controllers;
  managed Linux x86_64 guests; live proof is the released Rocky guest on LVM-thin.
- Invariants and assets: selected guest state is disposable only under approved
  exclusive gates; templates and other guests remain protected; controlled lookup,
  source pins, private logs, snapshot metadata and honest verification persist.
- Accepted failure classes: unavailable external repositories can stop preparation;
  existing errors must remain visible, partial state reported, and no proof claimed.
  No automated retry is added. A new failure requires diagnosis before mutation.
- Covered elsewhere: snapshot automation is implemented by merged #47; restore
  redesign belongs to a separately triaged finding; existing ownership, TLS, SSH,
  source admission and confirmation gates remain responsible for their boundaries.

## Threat model

- Boundary inventory: no new input boundary; command lookup widens only to three
  explicit standard system sbin directories. Source installation retains its pin.
- Actor model: controller caller environment is untrusted for lookup; guest operator
  and system package administrators are trusted as in the existing installation.
- Control per boundary: `env -i` and literal ordered PATH exclude inherited paths,
  empty entries and current-directory lookup; existing source-pin checks and private
  failure logs govern installation and errors. Tests verify this lookup contract.
- Explicitly out of scope: a compromised trusted guest administrator or approved
  upstream source is outside existing trust assumptions; restore redesign remains
  excluded and cannot be implemented as part of this repair.
