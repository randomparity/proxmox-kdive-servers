# ADR 0004: Separate guest configuration from source defaults

## Status

Accepted by the operator's request to fix template placeholders across CPU type,
NIC queues and guest settings, including default balloon enablement, on 2026-09-27.
Supersedes the source/guest hardware equality requirements of ADRs 0002 and 0003.

## Decision

Preserve exact source image/configuration ownership. Resolve original source
bridge, VLAN and storage before verifying existing templates. Do not compare source
placeholders with desired guest settings or recreate templates for guest changes.

Apply and verify guest CPU model, sizing, NIC model/queues, destination storage,
bridge/VLAN, balloon target and cloud-init values explicitly. Guest firmware and
secure-boot/storage layout remain enforced baseline requirements. Default balloon
target equals configured RAM; zero explicitly disables it. Reserve maximum RAM
and verify the driver and actual memory range. Include effective guest choices in
ownership, retaining legacy identity only for explicitly equivalent old settings.

## Consequences

Existing v1 template identities need no migration. Ready guests still reject drift;
this does not introduce automatic resizing or repair of used guests. Non-default
CPU models must pass the same actual nested-KVM proof. Source images stay unbooted.

## Considered & rejected

- Recreate templates for guest hardware changes: verified by the live VLAN mismatch
  and regression tests to be unnecessary; clones already receive explicit settings.
- Remove template integrity checks: judgment; placeholders do not justify accepting
  foreign images, disks or ownership markers.
- Inherit unspecified guest settings from source: verified regression tests expose
  CPU/queue overrides being ignored; explicit defaults prevent that coupling.
