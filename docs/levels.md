# Operator-captured levels

The level contract supports an ordered, closed registry above `clean`. The implemented
`toolchain` level prepares developer headers and tools, Docker Engine with Compose, local
libvirt/QEMU, and operator `uv`/`just`. Its parent is `clean`. `kernel-src` adds a pinned,
shallow, detached, unbuilt Linux source checkout above `toolchain`. `kdive` delegates installation
to a pinned upstream checkout above `kernel-src`. There is no configurable plugin loader or
test-level switch.

`LEVEL` defaults to `clean` for `make verify` and `make restore`. Existing clean metadata and
successful output stay unchanged. A higher level binds its exact native configuration, guest
identity, declared parent, digest of the parent's complete metadata, and recorded content.
Verification walks every ancestor, rejects RAM/incomplete snapshots, compares Proxmox ancestry,
checks root-owned 0644 manifests in `/var/lib/kdive-levels/`, and runs each guest check hook.
Recorded pins describe the snapshot; changing configured pins does not silently invalidate it.
Replacing a parent's metadata invalidates descendants and requires re-preparation.

For an installed higher level, `make level LEVEL=<registered-name> TARGETS=<alias>` produces a
read-only plan. Applying that reviewed plan requires `APPLY=1`, `CONFIRM` exactly matching
`TARGETS`, and `EXCLUSIVE=1` after consumers release the guest. The running guest must verify
at its parent, its native current parent must match, and the target snapshot must not exist.
Preparation verifies content and its manifest, then gracefully shuts down with no force-stop
fallback. Only after stopped-state validation does it print `READY TO SNAPSHOT <level> for
<alias>` and one JSON line containing the exact `level` name and `metadata` object.

The operator then captures a no-RAM snapshot using that exact name and the compact JSON
serialization of `metadata` as its description, through the Proxmox UI or `qm snapshot` with
`--vmstate 0 --description '<metadata JSON>'`. Start the guest before `make verify LEVEL=<name>`.
This manual path remains the default. To capture, boot and verify in the same operation:

```sh
make level LEVEL=toolchain TARGETS=ubuntu CAPTURE=1
make level LEVEL=toolchain TARGETS=ubuntu CAPTURE=1 APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
```

`CAPTURE=1` (or `scripts/guests.py --prepare-level --capture`) applies only to level preparation.
Without `APPLY=1` it is still a read-only plan. Capture uses the same confirmation and exclusive-use
requirements, checks the stopped guest, creates the selected no-RAM snapshot with the exact compact
prepared metadata, and reads back the native snapshot before starting the guest. It then verifies
the new boot and selected level before returning JSON with `action: captured` and snapshot evidence.
It does not print `READY TO SNAPSHOT` on this path. See [ADR 0016](adr/0016-optional-level-snapshot-capture.md).

The tool never renames, deletes or replaces higher-level snapshots, and never retries capture
automatically. A failure emits no success and leaves partial state inspectable. After shutdown,
a capture/readback failure retains the stopped guest; a later verification failure can leave it
running with a retained snapshot.
Inspect both guest and snapshots before explicitly restoring the parent and deciding whether to
remove a partial target snapshot. Re-running preparation refuses an existing target snapshot.
The current parent plus content checks prove the declared contract, not every unrelated disk byte.

Restore admits native chain metadata before stopping or rolling back, so a stopped guest or a
guest with damaged disk contents can recover. After rollback it boots and checks guest content.
On ZFS storage, newer snapshots block rollback to a lower level, including `clean`; admission
fails before shutdown and asks the operator to inspect/remove newer snapshots. It never removes
them automatically. The same check applies to the read-only restore plan. `lvmthin` storage
has no such restriction; see the [storage guidance](templates.md#verified-templates). Cooperative locks and
`EXCLUSIVE` cannot fence a separate privileged operator changing snapshots outside this tool.

## Toolchain preparation

```sh
make level LEVEL=toolchain TARGETS=ubuntu
make level LEVEL=toolchain TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
# Capture the emitted stopped, no-RAM snapshot using its exact metadata, then start the guest.
make verify LEVEL=toolchain TARGETS=ubuntu
make restore LEVEL=toolchain TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
```

Preparation installs distro packages, enables Docker and local libvirt, adds the configured
operator to `docker`, `kvm` and `libvirt`, and makes operator-owned `~/src` and its home safe
from group/other writes. It refuses symlinks, foreign ownership and unsafe system ancestors.
Docker membership grants broad guest privileges. Security enforcement stays enabled.
Operator login checks exercise GNU tools, Bash >=4.4, native build tools, headers, QEMU,
Docker/Compose and system libvirt, including operator Docker access. `realpath` may also be
uutils coreutils, which Ubuntu 26.04 installs by default; `find` and `grep` must be GNU, and
any other `realpath` provider fails preparation and verification.

The operator gets pinned `uv 0.12.19` from Astral and `rust-just 1.58.0` from PyPI with
`~/.local/bin` on the login PATH. The manifest records distro/release, critical tool versions,
and the hash of the sorted installed package inventory at capture. Additional descendant
packages are allowed; changing recorded critical versions fails verification.

Rocky preparation adds the operator-approved Docker stable RHEL repository and verifies its
signing key before import, preserving TLS and RPM signature checks. It refuses conflicting
configuration rather than replacing it. Rocky also uses stock RPM-verified CRB sources
(per toolchain transaction) and signed `epel-release` bootstrap from Rocky Extras for
EPEL 10.2 packages. DNF transactions select only BaseOS, AppStream, Extras, CRB, stable
EPEL and the approved Docker source, with TLS and package signature checks enabled.
Altered repository/key files, unowned EPEL configuration and duplicate source IDs are
refused. Source selection is reproducible; signed package revisions can advance and
are recorded in the installed inventory at capture. Rocky also installs and checks
`kernel-modules-extra` matching the running kernel, which pulls its matching modules;
installing only the newest kernel modules leaves the baseline kernel without Docker's
required `xt_addrtype` networking module.

Rocky gets `shfmt v3.14.1` from the
[official linux_amd64 release](https://github.com/mvdan/sh/releases/tag/v3.14.1),
with SHA-256 `76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf`.
The download uses HTTPS and a pinned checksum; no detached signature is claimed.
Preparation preserves conflicting `/usr/local/bin/shfmt` content. Verification checks
its digest, ownership/mode, operator login resolution and version instead of requiring
a Rocky shfmt RPM. Other distributions retain their packaged shfmt requirement.

openSUSE remains best-effort; this level does not imply KDIVE worker-host support. Missing packages
or service failures stop preparation for inspection; there is no automatic rollback.

openSUSE installs `polkit` explicitly so its existing libvirt group authorization policy
works without enabling recommended packages wholesale.

Native proof results and measured snapshot allocation are recorded in
[live proofs](live-proofs.md#toolchain).

## Kernel source level

`vars/kernel-source.json` supplies exactly `repo` and `ref` for preparation. The defaults
are the upstream stable HTTPS repository and `v6.9`, matching KDIVE's fetch helper. Edit
these inputs before preparing a new snapshot to select a different ref. Repositories must
use credential-free HTTPS; refs must be a full lowercase 40-hex commit or an unambiguous
exact tag/branch. Preparation resolves the ref once and records `repo`, `ref` and `commit`
in `/var/lib/kdive-levels/kernel-src.json` and the emitted snapshot metadata.

```sh
make level LEVEL=kernel-src TARGETS=ubuntu
make level LEVEL=kernel-src TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
# Capture the stopped guest with the exact emitted name/description and no RAM.
make verify LEVEL=kernel-src TARGETS=ubuntu
make restore LEVEL=kernel-src TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
```

The existing `toolchain` parent must verify first. Git runs as the operator account at
`~/src/linux`; preparation refuses an existing mismatching checkout without resetting or
removing it. A failed initial fetch can leave a partial tree: inspect it before retrying,
and let the operator decide its disposition. No package changes or kernel builds occur.
Verification uses recorded metadata offline; changing inputs or moving a remote tag does
not invalidate a captured level. It checks the real checkout and `.git` directories,
recursive ownership, detached matching HEAD, depth-one shallow history and clean status,
including ignored build products such as `.config` and object files. Deepening history,
configuring or building in this tree makes verification fail. Restore the captured level
or deliberately prepare a replacement; the tool does not delete trees or snapshots.

Native proof results and measured allocation are recorded in
[live proofs](live-proofs.md#kernel-source).

## KDIVE installed level

This level is warm-fixture preparation for later KDIVE validation entries
([KDIVE #2808](https://github.com/randomparity/kdive/issues/2808) through
[#2817](https://github.com/randomparity/kdive/issues/2817)). It is not KDIVE #2807
clean-host installation evidence, and its proof does not replace one. Run installation proofs
on the [clean-only installation guests](snapshots.md#clean-only-installation-guests). The level differs
from a clean-host installation in these ways:

- It sits above `toolchain`, which already installs libvirt/QEMU, Docker and the operator's
  group memberships that the KDIVE host play is expected to provide.
- It installs `python3-packaging` itself before the host play.
- It runs `ansible-playbook` directly, not the documented upstream KDIVE
  `examples/local-libvirt/install-host.sh` entry point.
- It performs no real guest provision/boot and no repeat setup; its proof is not a nested VM
  provisioning or kdump proof.

The `python3-packaging` install and the direct playbook invocation live only in
`prepare_kdive` in `scripts/guest_verify.py`. No other path, including installation-proof
tooling, reuses them.

`vars/kdive-source.json` selects an HTTPS repository and full commit for preparation only.
The checkout lives at the operator's `~/src/kdive`; mismatching or dirty existing trees are
preserved and refused. Preparation delegates the runtime setup recipes listed below and
the upstream local-libvirt host play. It requires installed Python 3.14 and its development
headers before `just sync`, which builds native Python bindings. On Rocky, install the stock
`python3.14` and `python3.14-devel` packages after capturing `kernel-src` and before
preparing `kdive`. Also install matching `libguestfs-devel` from the existing CRB
repository (`sudo dnf --enablerepo=crb install python3.14 python3.14-devel libguestfs-devel`);
this command does not persistently enable CRB. The configured distribution source
repository must supply the matching signed libguestfs source RPM. The upstream play
provides the Python 3.14 `guestfs` binding from that source.
Ubuntu 26.04, Fedora 44 and Rocky 10.2 have completed installed-level proofs.
openSUSE is excluded. The dated Rocky LVM-thin restore result is recorded in
[live proofs](live-proofs.md#lvm-thin-restore-proof-2026-10-02).

```sh
make level LEVEL=kdive TARGETS=fedora
make level LEVEL=kdive TARGETS=fedora APPLY=1 CONFIRM=fedora EXCLUSIVE=1
# Take the exact no-RAM snapshot only after READY, using its emitted metadata.
make verify LEVEL=kdive TARGETS=fedora
make restore LEVEL=kdive TARGETS=fedora APPLY=1 CONFIRM=fedora EXCLUSIVE=1
```

Preparation requires an exclusive guest with no existing Docker containers/volumes or
installed KDIVE lifecycle state. It creates an owned disposable local backend project;
Postgres, SeaweedFS and OIDC publish only on guest loopback. There is no external witness
DSN input. Captured witness credentials belong only to this disposable guest. Successful
stop preserves local volumes; it independently verifies no host daemons or workers remain.

Preparation invokes upstream `just sync`, `just build-capture-bootstrap-manifest` and
`just install-ansible-collections` in order. It does not run developer setup or install
Git hooks, so it preserves the ancestor's selected tools. The host play receives a fixed
system-only PATH through Ansible's sudo options, including `/usr/local/bin` for root's
installed `uv`; it does not change sudoers or pass the operator's user-local PATH to root.

Installer failures retain root-private logs under `/var/lib/kdive-levels/kdive-state` and
withhold READY. Inspect them privately; they can contain credentials. Automatic retries do
not replace partial installations, snapshots, or ancestors. Preparation may take up to four
hours. Any critical ancestor tool-version drift fails verification.

The manifest and snapshot content record `kdive_sha`, `kernel_commit`, repository and a hash
of non-secret play inputs. Verification also reports the recorded `kdive_sha`; it does not
compare against a newly selected default. Verification expects the stack stopped, with its
lifecycle socket enabled. To use the captured deployment after restore, start a clean shell
as its operator with the same Compose identity and loopback override:

```sh
cd ~/src/kdive
env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" \
  PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin" LANG=C.UTF-8 \
  DOCKER_HOST=unix:///var/run/docker.sock COMPOSE_PROJECT_NAME=kdive-level \
  COMPOSE_FILE="$PWD/docker-compose.yml:/var/lib/kdive-levels/kdive-state/compose.yml" \
  UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 bash --noprofile --norc
scripts/live-stack/stack-services.sh --skip-obs
source scripts/live-stack/env.sh
export KDIVE_STACK_SKEW_POLICY=strict
uv run --no-sync python -m pytest \
  tests/integration/test_live_stack.py::test_viewer_denied_operator_op_over_the_wire \
  -m live_stack --strict-markers -q
scripts/live-stack/stack-down.sh
```

Use this same environment for start and stop; plain upstream commands from another login
can select different volumes or publications. Require exactly one pass and no skips for this
bounded real HTTP authorization proof. It is not a nested VM provisioning or kdump proof.
Before capturing, confirm daemon/worker stop and gracefully shut down the guest.
Boot the guest before `make verify`; keep the KDIVE stack stopped during verification.

KDIVE preparation fetches genuine upstream `main` into `origin/main` for the upstream setup
schema guard and requires that ref to equal the explicitly approved source pin. If main
has advanced, preparation stops for source selection; it never advances the pin or bypasses
the guard. Verification and restore use recorded installed content without this network check.

KDIVE preparation installs `python3-packaging` from the guest's configured repositories
and checks its import in `/usr/bin/python3` before invoking the host play. This is the
Ansible pip module's interpreter prerequisite, owned by this integration; it does not
change the ancestor toolchain package contract or replace upstream installer tasks.

Native proof results are recorded in [live proofs](live-proofs.md#kdive-installed-level).
