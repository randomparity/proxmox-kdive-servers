# Ubuntu installed KDIVE proof

## Scope and authority

Issue #25 (cycle 2, scope token q25-930ccfa7) authorizes a supported Ubuntu route and its
real installed-level proof. The operator approved the Ubuntu 26.04 release-20260918 image,
preserved-old replacement resources and a new network allocation. One then-current KDIVE
main commit may be resolved immediately before KDIVE preparation and frozen through proof.
The existing source guard remains enforced. No new product capture command is introduced.
After the first cycle's live failure the operator approved an expansion: an Ubuntu
`cloudinit_snippet_storage` input and a native custom network-only seed matched by MAC
without renaming. Upstream installer/binding implementation belongs to KDIVE maintainers;
other distro lifecycle changes belong to separate issues/operator; warm caches and full
nested VM/kdump proof belong to future dedicated work/operator. These approved exclusions
remain unchanged. Decision record: [ADR 0012](../../adr/0012-ubuntu-kdive-image.md).

## Image and source

Replace the Ubuntu image record in `vars/images.json` with the approved upstream image,
its checksum, size and read-only inspected baseline. Template identity hashing refuses reuse
of old image identities. The inspected `python3` metapackage is 3.14.3; Ubuntu's matching
`python3-guestfs` is installed by the upstream installer and proven by importing it with the
actual installed lifecycle interpreter. `scripts/guest_verify.py` keeps setup/installer
ownership, clean environment, strict main-equality guard, loopback backend override and
recorded ancestor checks unchanged. `vars/kdive-source.json` changes only at the approved
single source-selection moment.

## Network seed

Evidence: on the first fresh 26.04 guest, cloud-init 26.1 refused to rename the already
active DHCP interface to `eth0`, which Proxmox's generated NoCloud network config always
requests (`set-name`), so the static address never came up. Proxmox replaces only the
generated network document when `cicustom` names a `network=` snippet; user-data (user,
keys, hostname) and meta-data stay native.

Inventory. `cloudinit_snippet_storage` is a native storage identifier. It is required for
the `ubuntu` profile and rejected for every other profile (their lifecycle is excluded).
`scripts/validate_inventory.py` validates it offline; `scripts/guests.py` forwards it in the
guest request. `inventory/example.yml` shows it for the Ubuntu host.

Identity. `guest_host.identity` adds `cloudinit_snippet_storage` to the guest overrides only
when present, so Fedora, Rocky and openSUSE identities, markers and snapshots are unchanged.
The Ubuntu identity changes; no existing Ubuntu guest is adopted.

Seed document. `network_seed(request, mac)` returns deterministic UTF-8 bytes: a first-line
comment `# kdive-guest-network-v1 identity=<guest identity>` followed by a cloud-init v2
document with one `ethernets` entry `kdive0` whose `match.macaddress` is the cloned NIC MAC
in lowercase, `dhcp4: false`, the inventory `ipv4_cidr`, a default route via `gateway`, and
`nameservers` with `dns_servers` and the FQDN's domain as `search`. It has no `set-name`.
Every value is already validated inventory data or a MAC matching the existing strict
pattern. The file name is `kdive-net-<vmid>-<sha256 of the bytes>.yaml`; the volume is
`<storage>:snippets/<name>`; the guest option is `cicustom: network=<volume>`. Because the
name carries the content hash and the content carries the guest identity, a file is never
rewritten: a different MAC, address or identity yields a different name.

Host admission. For a request with the field, `guest_host.admission` requires the snippet
storage's native node status to be `active == 1`, `enabled == 1` with `snippets` in its
content list, for every mode. The product never changes storage configuration. The seed
directory is the parent of `pvesm path <volume>`; it must be an existing real directory
owned by the executing user (root on the host) and not group- or world-writable.

Exclusive write. `write_seed(request, config)` runs in `clone` after the MAC is known and
before the single `qm set` that applies managed options. It opens the directory with
`O_DIRECTORY|O_NOFOLLOW`, creates a hidden temporary file with `O_CREAT|O_EXCL|O_NOFOLLOW`
mode 0600, writes and fsyncs it, then publishes with a no-replace hard link to the final name
and removes the temporary name. An existing final name is accepted only when it passes
`verify_seed` byte-for-byte; otherwise provisioning stops without writing.

Verification. `check_configuration` requires `cicustom` to equal the value derived from the
current `net0` MAC. In phase `preparing` only, an absent `cicustom` is tolerated (a partial
clone that failed before `qm set`); a present one must match. `verify_seed` requires the
final file to be a regular non-symlink, owned by the executing user, one link, no group or
world write bit, and bytes exactly equal to `network_seed`. `inspect_guest` calls it whenever
`cicustom` is present, which covers readiness, verify, level preparation, restore after
rollback, teardown admission and the inspection preceding every product `qm start`. Snapshot
configs already must normalize equal to the current config, so they carry the same
`cicustom`.

Deletion. Teardown computes the expected seed from the pre-destroy config's MAC. After
`qm destroy` and the existing absence checks (guest gone from cluster resources, owned
volumes gone; snapshots are destroyed with the guest), `release_seed` builds a reference
inventory from every `*.conf` under `/etc/pve/nodes/*/qemu-server/` and
`/etc/pve/nodes/*/lxc/` (current, pending and snapshot sections). The inventory is complete
only if every file reads and the admitted node's `qemu-server` directory contains the source
template's config. It unlinks the seed only when the file exists, still passes `verify_seed`,
and no inventory file contains its name. A remaining reference, unreadable inventory or
changed bytes retains the file and fails with an actionable message after the guest is gone.

## Failure model

1. Actors and deployments
   - Local operator on an x86_64 or ARM64 controller running `make` targets over pinned SSH.
   - Product host code as root on one Proxmox VE 9 node with an operator-enabled dir snippet
     storage; one exclusive Ubuntu replacement guest.
2. Invariants and assets at stake
   - Existing guests, templates and snapshots, including the preserved old Ubuntu resources.
   - Non-Ubuntu identities and snapshots unchanged; Ubuntu level chain clean → toolchain →
     kernel-src → kdive with operator no-RAM captures.
   - Seed bytes a guest boots from: never overwritten, never deleted while referenced.
   - Credentials, image integrity, enforcing AppArmor, local disposable backends.
3. Accepted failure classes
   - Another root actor editing the snippet directory between verification and `qm start`:
     root on the host is trusted; the next inspection fails closed.
   - A failure after destroy leaves a retained seed file: bounded (one small file), reported.
   - Missing or altered seed on an existing guest refuses verify/restore/teardown until the
     operator inspects: fail-closed by design.
   - Upstream or repository unavailability during preparation stops with private diagnostics.
   - A source advance after the single approved pin stops at the existing guard.
   - Resource shortages stop before installation.
4. Covered elsewhere
   - Upstream installer defects — KDIVE maintainers.
   - Snippet storage enablement — operator host configuration.
   - Other distros' network seeds — separate issues/operator.

## Threat model

- Boundaries added: product-authored files in a hypervisor snippet directory read by Proxmox
  at every start; widened: guest options now reference a host file.
- Actors: the local operator (trusted, supplies inventory); root on the host (trusted);
  guest OS (untrusted, cannot write host files).
- Controls: offline inventory validation of every seed input; content derived only from
  validated fields and a pattern-checked MAC; exclusive no-follow writes; owner/mode/link and
  exact-byte checks before use; content-hash names prevent substitution; deletion gated on a
  complete reference inventory. Failures print fixed messages without supplied values.
- Out of scope: a malicious root on the host; secrets (the seed contains none).

## Success and evidence

Read-only image inspection proves digest, virtual size, OS, packages, EFI and NoCloud. Fresh
provisioning reaches the static address over SSH with the kernel interface name unchanged,
completes the clean baseline, and a template rerun preserves identity. Each ancestor is
prepared, operator captured without RAM after READY, verified and restored; KDIVE follows
the same cycle. Record Python 3.14 and guestfs import from the installed lifecycle
interpreter. After KDIVE restore, start the installed stack without setup/Ansible, run the
documented HTTP authorization test requiring one pass and zero skips, stop the stack, verify
and stop the guest. Record restore/test timings and native snapshot growth. Replace the Ubuntu
deferral in README only after this proof; document the snippet storage prerequisite.

## Validation

Unit tests cover inventory acceptance/rejection of the new field; unchanged non-Ubuntu
identity; deterministic seed bytes without `set-name`; exclusive write, idempotent re-use and
refusal of differing/symlinked/unsafe files; `cicustom` and byte checks on verify, restore
and start; snippet storage admission; and teardown deletion versus retention on references
or unreadable inventory. The live Ubuntu proof is the integration evidence for the seed and
guestfs; no mock establishes native compatibility. `make check` gates each commit.
