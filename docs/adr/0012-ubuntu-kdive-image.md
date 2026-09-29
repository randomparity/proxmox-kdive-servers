# ADR0012: Use Ubuntu 26.04 for the installed KDIVE level

## Status

Accepted by operator approval for issue #25 on 2026-09-28; network seed decision accepted
by operator approval of the scope expansion on 2026-09-29.

## Context

KDIVE requires installed Python 3.14 and a matching native guestfs binding. The prior
Ubuntu image was 24.04 with Python 3.12, and its installed proof was deferred.

On the first fresh 26.04 guest, cloud-init 26.1 refused to rename the already active
DHCP interface to `eth0`. Proxmox's generated NoCloud network config always requests that
rename, so the static address never came up and no clean baseline was produced.

## Decision

Pin the approved Ubuntu 26.04 release-20260918 image and record its read-only inspected
baseline. Rebuild the level chain in separate operator-assigned replacement resources,
preserving the old guest, template and snapshots. Delegate matching native bindings to
the upstream installer using Ubuntu packages. Resolve one approved current main commit
immediately before KDIVE preparation; retain that pin and the existing equality guard.

For Ubuntu guests, supply the network document through native `cicustom` from an
operator-enabled snippet storage named by a new `cloudinit_snippet_storage` inventory
input. The product writes one immutable seed per guest, matched by MAC without renaming,
named by its content hash and carrying the guest identity, verifies it before use and
deletes it only after the guest is gone and no configuration references it.

## Considered & rejected

- Keep the old image: verified: vars/images.json at ed3a224 records Ubuntu 24.04 and
  Python 3.12.3; issue #25 requires installed Python 3.14/native guestfs proof.
- Add an interpreter/binding build: judgment: duplicates upstream installation ownership
  and expands maintenance beyond the approved image migration.
- Replace resources in place: judgment: preserving the old resources makes this bounded
  proof easier to reverse and follows the operator's explicit replacement approval.
- Native v2 NoCloud network output: verified: `nocloud_network_v2` in
  `PVE/QemuServer/Cloudinit.pm` (pve-manager 9.2.20) also emits `set-name: eth<id>`.
- ConfigDrive seed: verified: `configdrive2_network` in the same file emits legacy ENI for
  `eth0`, and cloud-init's ConfigDrive source converts it without MAC matching.
- Reboot after the failed first boot: judgment: a workaround that leaves fresh provisioning
  failing and bakes an untested second boot into the clean baseline.
- Modify the pinned image or template cloud.cfg: judgment: breaks the immutable upstream
  image pin and template identity for a per-guest network choice.
- Custom user-data as well as network: judgment: replaces the native user and key seeding
  the product already verifies, for no gain.

## Consequences

Image-dependent identities and snapshots must be rebuilt; previous captures are not
compatible metadata for this image. The source pin also changes the next preparation
for other supported distributions, while verification uses recorded installed content.
A moving upstream main can still prevent preparation after selection; failure does not
permit advancing the pin. The bounded HTTP proof is not a nested VM or kdump proof.

Ubuntu provisioning now needs a snippet-capable storage configured by the operator, and
Ubuntu guest identities change. A missing or altered seed stops verify, restore and
teardown until inspected. Teardown can leave a seed file behind when references remain;
it fails loudly rather than deleting. Other profiles are unaffected.
