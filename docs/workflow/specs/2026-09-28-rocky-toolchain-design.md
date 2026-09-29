# Rocky toolchain sources

## Problem and authority

Issue #26 cannot reach toolchain READY: its RPM list requires libvirt-devel,
ShellCheck and shfmt while only the existing Rocky and Docker sources are used.
The operator approved CRB, signed EPEL 10.2 bootstrap and the official shfmt
v3.14.1 x86_64 binary pinned by SHA-256; scope token q26-ac8c9370 freezes authority.
[ADR 0011](../../adr/0011-rocky-toolchain-sources.md) records the source decision.

## Design

Extend the existing guest verifier, which already owns toolchain installation and
observation. No ownership transition or controller/schema change is needed.
Use stock RPM-verified Rocky repository/key files. Reject altered files, symlinks,
and duplicate CRB/EPEL identifiers before installation. Enable CRB per transaction;
do not rewrite its configuration. Bootstrap epel-release from Rocky Extras with
signature/TLS checking, validate installed EPEL repository/key files against their
RPM records, and use stable EPEL only. Refuse unowned pre-existing EPEL files.
Limit Rocky installation transactions to approved repository identifiers and force
package signature/TLS checks. EPEL package revisions follow moving upstream
metadata; RPM signatures are checked; package inventory digest records the installed versions as before.

Remove shfmt only from Rocky's RPM list, retaining its executable requirement.
Install the approved binary into /usr/local/bin/shfmt only when absent, using a
private download, HTTPS-only redirects, digest validation, root ownership and
executable mode. Existing content must already match the approved digest and be a
regular root-owned non-group/world-writable executable; otherwise preserve/refuse.
Observe the same digest and require the operator login to resolve that exact path
and report v3.14.1. Other distributions retain their package requirements.

## Failure model

- Covered: missing repositories/packages/tools/groups; conflicting source files,
  duplicate source IDs, changed shfmt or shadowed login path; refuse actionably.
- Accepted: upstream metadata can move signed package versions. The snapshot
  records installed inventory; this is reproducible source selection, not a frozen mirror.
- Excluded: compromised root/RPM database, upstream installer repairs and downstream
  kernel/KDIVE execution; owners are the operator or upstream maintainers per charter.
- Recovery: preparation can leave inspectable partial state; no automatic rollback,
  capture or deletion. Only separately approved operator lifecycle actions reset it.

## Success and validation

Focused tests cover bootstrap admission, conflict refusal, selected sources,
binary hash/type/mode/version/PATH checks and Rocky-only RPM replacement.
Existing missing-tool/group tests remain and live failures must cross the public
controller boundary. Run make check and independent design/code/security review.
On the specifically authorized Rocky guest: clean rollback, prepare READY, operator
no-RAM toolchain capture, verify/restore, shfmt removal and docker-group removal
rejections separated by restore, reverify, record storage growth and leave stopped
with SELinux enforcing. README reports actual revision/results and downstream status.
No live action is authorized by this document alone.
