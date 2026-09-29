# Kernel source snapshot level

## Authority and scope

Issue #19, epic #16 and frozen scope `q19-930c48ab` govern this level. The operator
approved the concrete proposal on 2026-09-28. ADR 0009 records the input/provenance
boundary. Extend the existing registry and hooks; no ownership transition is needed.
Full history belongs to the operator; builds/caches to KDIVE; installation to #20;
resetting or removing existing trees and snapshot replacement to the operator.

## Behavior

Register `kernel-src` with parent `toolchain`. Read `vars/kernel-source.json`
only for kernel-src preparation: exactly repo/ref, defaulting to
`https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git` and `v6.9`.
Pass these as optional `kernel_source` request data; validate at controller, native
request and guest boundaries without changing baseline identity. Other operations
retain their request shape. Native admission permits the optional validated field;
only the kernel prepare hook consumes it.
Accept credential-free HTTPS URLs without query, fragment, whitespace or control
characters. Accept a lowercase 40-hex commit or exact Git ref name without glob
or option syntax. Resolve branch/tag candidates with `ls-remote`, prefer peeled
annotated-tag commits, reject conflicting branch/tag resolution. The ref is resolved
once before a new checkout. The observed upstream v6.9 commit on 2026-09-28 is
`a38297e3fb012ddfa7ce0321a7e5a8daeb1872b6`; this observation does not replace runtime
resolution. Record content exactly `{repo, ref, commit}` in the existing manifest.
Verification and restore read recorded content without network/current-input checks.

Run Git through the existing fresh operator login using quoted argv. Restrict Git
transport to HTTPS, disable prompts, hooks and fsmonitor for these commands, and
retain fixed public-safe operation errors. Bounded command timeouts include a
900-second fetch; no command logs disclose external responses or private paths.
Validate the existing operator-owned, non-group/world-writable home/src chain.
If linux exists, require a real directory and real local .git directory, then
inspect without resetting, fetching, chmod/chown or deleting. If absent, run
`git init`, `git fetch --depth=1 -- <repo> <commit>` and detached checkout as the
operator, matching KDIVE fetch-kernel-tree semantics except stricter ignored checks.

Check directory and descendant ownership with lstat, without following symlinks;
tracked source symlinks remain valid, but linux/.git symlinks or gitdir indirection
are rejected. Require checkout root equal to the destination, detached HEAD equal
to recorded commit, shallow state and one commit reachable from HEAD. Require empty
`git status --porcelain --untracked-files=all --ignored`; thus ignored .config and
build outputs fail as well as tracked changes and untracked files. Check the manifest
via existing root-owned atomic level machinery. Ancestor checks run unchanged before
and after preparation. Do not install packages, configure/build kernels or change tools.

## Failure model

- Actors and deployments: authenticated operator/controller; exclusive preparation
  window; four pinned x86_64 Linux guest profiles, Linux/macOS controllers.
- Invariants/assets: preserve existing trees and lower snapshots; recorded unbuilt
  depth-one source; account ownership; ancestor baseline and package contracts.
- Accepted failure classes: network/disk/timeouts can leave a partial new checkout;
  refuse it on retry and require operator disposition. Concurrent hostile filesystem
  mutation is outside the granted exclusive window. Repository availability after
  capture is not required. Arbitrary malicious Git internals are outside the trusted
  operator account model; pre-existing symlink/ownership mistakes remain covered.
- Covered elsewhere: snapshot lifecycle/manifest binding/locks by ADR 0007 and
  existing level code; toolchain by ADR 0008; upstream install/build by KDIVE.

## Threat model

- Assets: guest filesystem, snapshot provenance and private transport diagnostics.
- Untrusted inputs: repo/ref config, remote refs, existing tree/metadata. Validate
  finite content schema and commit shape, exact ref resolution and HTTPS scheme;
  execute Git as operator with argv quoting, never a remote-supplied shell command.
- Boundaries: controller to native request to guest RPC; root-owned manifest versus
  operator-owned tree. Inspect ownership before Git and mask unknown failures.
- Out of scope: compromised root/operator or concurrent malicious changes during
  exclusivity; remote source semantic safety is chosen by the operator's repo/ref.

## Validation and live proof

Real temporary Git repositories exercise detached/shallow/ownership/dirty/ignored
checks and preservation. Stub only external fetch/remote resolution and account/login
boundaries. Tests cover malformed schemas, unsafe URLs/refs, peeled and conflicting
refs, incomplete paths, non-shallow history, wrong HEAD, branch checkout, tracked and
ignored/untracked changes, nested ownership, offline recorded-content verification,
controller forwarding/diagnostic masking and registry integration. Prove tests bite.
Run make check on the assembled candidate and Linux/macOS CI.

Approved live scope is Ubuntu/Fedora only, sequential on the host lock. Verify the
existing toolchain, prepare to READY, confirm stopped, and give the root orchestrator
exact emitted no-RAM capture metadata. Then verify; inject untracked file, ignored
.config, wrong HEAD and wrong owner separately, demanding failure each time and
restoring kernel-src/reverifying after each. Measure native allocated delta against
toolchain. Finish stopped retaining clean/toolchain/kernel-src. No lower restore,
snapshot deletion, existing-tree replacement, package upgrades or forced stop.
