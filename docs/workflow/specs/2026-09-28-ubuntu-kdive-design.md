# Ubuntu installed KDIVE proof

## Scope and authority

Issue #25 and scope token q25-a1f70aa0 authorize a supported Ubuntu route and its real
installed-level proof. The operator approved the Ubuntu 26.04 release-20260918 image,
preserved-old replacement resources and a new network allocation. One then-current KDIVE
main commit may be resolved immediately before KDIVE preparation and frozen through proof.
The existing source guard remains enforced. No new product capture command is introduced.
Upstream installer/binding implementation belongs to KDIVE maintainers; other distro work
belongs to separate issues/operator; warm caches and full nested VM/kdump proof belong to
future dedicated work/operator. These approved exclusions remain unchanged.

## Design

Replace the Ubuntu image record in vars/images.json with the approved upstream image,
its checksum, size and read-only inspected baseline. Existing template identity hashing
naturally refuses reuse of old image identities. Use separate private inventory and pins
for the replacement; preserve the previous guest, template and snapshots stopped.
The image contains system Python 3.14.3. Use Ubuntu's matching python3-guestfs through the
upstream installer, then prove import using the actual installed lifecycle interpreter.
Retain scripts/guest_verify.py's setup and installer ownership, clean environment, strict
main equality guard, loopback backend override and recorded ancestor checks unchanged.
Update vars/kdive-source.json only at the approved single source-selection moment.
No caller migration or ownership transition is required: scripts/templates.py consumes
image provenance and scripts/guests.py consumes source selection through existing records.

## Failure model

Named deployment: one exclusive x86_64 replacement guest on a capacity-checked Proxmox host,
with ARM64 or x86_64 controller support unchanged. Assets are old guests/snapshots, credentials,
image integrity, enforcing AppArmor and the clean → toolchain → kernel-src → kdive chain.
Checksums, readiness, parent binding, stop or security failures withhold READY/capture.
Upstream or repository unavailability is accepted as a stop with private diagnostics; this
proof does not promise offline installation. A source advance after the one approved pin
stops at the existing guard; it cannot authorize a second pin. Diagnosed retries may reset
only campaign-created resources with operator capture coordination. Existing snapshots
are never relabeled for a changed image. Upstream installer defects are reported to their
owner, not patched in this repository. Resource shortages stop before installation.

## Success and evidence

Read-only image inspection proves digest, virtual size, OS, packages, EFI and NoCloud.
Existing clean provisioning and template rerun complete, then each ancestor is prepared,
operator captured without RAM after READY, verified and restored. KDIVE follows the same
cycle. Record Python 3.14 and guestfs import from the installed lifecycle interpreter.
After KDIVE restore, use the documented clean environment and local Compose override;
start the installed stack without setup/Ansible, execute the documented HTTP authorization
test requiring one pass and zero skips, stop stack, verify and stop guest. Record controller
restore/test timings and native snapshot written/referenced growth. Replace the Ubuntu
deferral in README only after this proof; preserve historical Fedora measurements.

## Validation

Regression tests assert the selected Ubuntu image supports system Python 3.14, uses the
approved immutable upstream release and fresh image identity. Existing template identity,
provisioning, security, ancestor, restore and source-guard tests remain authoritative.
The actual imported image and installed proof are the integration evidence; no mock
can establish native guestfs compatibility. Run make check through the required hook for
implementation commits. Design prose is reviewed without invented wording assertions.
