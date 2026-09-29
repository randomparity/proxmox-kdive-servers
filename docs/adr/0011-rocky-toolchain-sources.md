# 0011: Rocky toolchain sources

## Status
Accepted by operator source approval, 2026-09-28.

## Context
Issue #26 records missing libvirt-devel, ShellCheck and shfmt under the existing
Rocky 10.2 sources. Required capabilities must not disappear to make READY pass.

## Decision
Use Rocky CRB and signed EPEL bootstrap from Rocky Extras. Keep vendor repository
files intact and reject conflicting configuration. Use the operator-approved
shfmt v3.14.1 linux_amd64 HTTPS release binary, SHA-256
`76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf`.
Verify the installed file and operator resolution/version on preparation and
verification. This replaces Rocky's RPM-name requirement only.

## Consequences
Adds EPEL trust and a checksum-pinned binary; no detached signature is claimed.
Signed package revisions can advance; existing inventory provenance
records installed versions. Conflicting configuration/content requires operator
repair rather than overwrite. CRB is enabled for toolchain transactions only.

## Considered & rejected
- Drop unavailable requirements: judgment: violates the requested capability contract.
- Keep only existing sources: verified: issue #26 records DNF missing-package error
  at a6ae7c5dd36575a1e87e3fe5f7e098e7da35a199 under Rocky 10.2.
- Build shfmt from source: judgment: adds a compiler/bootstrap path beyond the approved binary.

Sources: [Rocky repository guidance](https://wiki.rockylinux.org/rocky/repo/)
and [shfmt v3.14.1 release](https://github.com/mvdan/sh/releases/tag/v3.14.1).
