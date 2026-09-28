# Toolchain snapshot level

## Authority and scope

Issue #18, epic #16 and scope token `q18-466d5540` authorize a speed layer above
`clean`. The operator approved four-guest preparation and the signed Docker
stable RHEL repository on 2026-09-28. ADR 0008 records the package policy.
Use #17's existing root guest RPC and level hooks without changing its protocol.
Kernel trees belong to #19; KDIVE installation and its complete evolving host
contract belong to #20/upstream. Capture, deletion and replacement remain operator
operations. No images, caches or built kernels are added.

## Behavior

Register `toolchain` with parent `clean`. Prepare installs the packages below,
configures the existing operator, and observes content. Check independently
observes required capabilities and compares recorded critical versions. Baseline
security and readiness still run before and after hooks through `run_level`.
No service listens remotely as a result of explicit configuration in this change.
The existing controller masks failed guest stderr. For level envelopes only, map a
finite list of exact tool/group errors to controller-owned actionable remediation;
unknown diagnostics remain masked. This necessary direct-caller adjustment meets
#18 fault-message criteria without new RPC fields or emitting guest-controlled text.

Common packages: bash, coreutils, findutils, grep, git, curl, ca-certificates.
Native developer lists follow KDIVE `docs/operating/install.md`:

| Profile | Developer packages | Runtime packages |
|---|---|---|
| Ubuntu | build-essential pkg-config libvirt-dev python3-dev libelf-dev shellcheck shfmt | libvirt-daemon-system libvirt-clients qemu-system-x86 docker.io docker-compose-v2 |
| Fedora | gcc make pkgconf-pkg-config libvirt-devel python3-devel elfutils-libelf-devel ShellCheck shfmt | libvirt libvirt-client qemu-kvm moby-engine docker-compose |
| Rocky | Fedora developer list | libvirt libvirt-client qemu-kvm docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin |
| openSUSE | gcc make pkg-config libvirt-devel python3-devel libelf-devel ShellCheck shfmt | libvirt-daemon-qemu libvirt-daemon-proxy libvirt-client qemu-x86 docker docker-compose |

Install from configured distro repositories, without adding new sources except
Rocky's approved Docker stable RHEL repository. Verify its signing key against
`060A61C51B558A7F742B77AAC52FEB6B621E9F35`; retain package signature and TLS checks.
Conflicting repository configuration fails before replacement. Missing packages
on Rocky/openSUSE are exact documented gaps, not permission to add another repo.

Install official pinned `uv 0.12.19` for the operator and use it to install
`rust-just==1.58.0` from PyPI. Versioned installer runs as that operator, never
root. Preserve shell profiles while ensuring `~/.local/bin` is on login PATH.
Release versions were resolved from official release/PyPI metadata on 2026-09-28.
Enable/start Docker and local libvirt service or modular sockets supplied by the
profile. Add operator to existing/created docker, kvm and libvirt system groups.
Create operator-owned `~/src`; remove group/world write on src and home, refusing
symlinks/foreign ownership and unsafe system ancestors instead of changing them.

Observe binaries from a fresh operator login shell: bash (>=4.4), GNU coreutils,
findutils and grep, git, curl, compiler, make, pkg-config, Python, shellcheck,
shfmt, QEMU, virsh, uv, just, Docker and Compose. Query required package installation
independently so headers are covered. Require effective docker/kvm/libvirt groups,
Docker enabled/active and operator `docker info`, operator system-libvirt version,
and safe source path. Errors name the failing capability and a remediation.

Content records distro/release, uv/just/docker/libvirt versions and SHA-256 of
sorted installed package identifiers. This whole-package hash is capture-time
provenance, not a ban on packages added by descendants. Required tools and recorded
critical versions remain independently checked; snapshot metadata binds content.
Reuse #17's root-owned atomic manifest write and snapshot comparison.

## Failure model

Deployments are the four pinned x86_64 Linux guests, with their existing operator
account, an exclusive preparation window, authenticated RPC and distro managers.
Assets are clean snapshots, enforcing security, account-owned source paths and
trusted packages. No snapshot mutation occurs inside product code.
Package/download/service errors stop with the failing operation; preparation can
leave partial installed state. Recovery is operator inspection or approved restore,
not automatic rollback or package removal. Unavailable distro packages are allowed
reported gaps only for Rocky/openSUSE; Ubuntu/Fedora must pass before merge.
Concurrent hostile operator filesystem changes are outside the exclusive window;
pre-existing symlink/ownership mistakes are inside it and must fail closed.
Package repositories changing versions after capture do not modify captured disks;
descendant upgrades of recorded critical tools fail verification until deliberately
re-prepared. Additional descendant packages are allowed.

## Validation

Focused tests cover package maps/commands, source verification, login execution,
version floors, package/header absence, group removal, paths, manifest content and
added-package tolerance. Test new checks with deliberate faults before restoring.
Run configured lint and full `make check` on assembled candidate; Linux/macOS CI
checks controller portability. Do not infer guest behavior from mocked commands.
For Ubuntu/Fedora: restore clean as needed, prepare to READY, confirm stopped,
provide exact emitted metadata to orchestrator for operator-authorized capture,
verify, hide just then require verify failure and restore/reverify, remove docker
membership then require failure and restore/reverify. Gracefully stop afterward.
Attempt Rocky/openSUSE under the same bounded authority, reporting exact gaps.
Retain clean and valid toolchain snapshots. Measure native allocated growth versus
clean and publish sanitized measurements. An unexpected failure prompts diagnosis;
no snapshot deletion/replacement or forced stop is authorized.
