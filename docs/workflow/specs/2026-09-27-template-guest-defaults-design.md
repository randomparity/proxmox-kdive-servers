# Separate template defaults from guest configuration

## Scope and authority

The operator requested: “The template values are placeholders. We're already
modifying CPU/RAM/DISK, let's fix it correctly,” then explicitly expanded this to
“CPU type, NIC queues, etc.” Outcome: complete guest-setting isolation from source
placeholders and live proof using exactly the operator's private `lab.yml`.
Branch `refactor/simplify-distro-names`, base `main`; complexity L (1000-line design
denominator), full-spec lane for persisted ownership and external-service behavior.

## Behavior and ownership

Templates retain verified image provenance, exact original v1 identity, disk layout,
firmware and unbooted configuration. Their CPU/RAM/NIC settings are source defaults,
not guest constraints. Resolve existing source bridge, VLAN and storage from native
configuration and validate the original identity/configuration before reuse. New
sources have fixed CPU/RAM and an untagged placeholder NIC; destination guest sizing
and CPU never enter template identity. Preserve all existing template markers.

Guest inventory controls CPU model (`cpu`), cores, RAM, root size, storage, bridge,
VLAN, NIC model (`nic_model`, default virtio), NIC queues (`nic_queues`, optional
integer 0–64 per installed Proxmox schema), hostname/domain, IPv4/gateway/DNS,
account and SSH keys. Queues require virtio. CPU accepts literal model names;
actual nested KVM remains mandatory even for a non-host model. Omitted NIC queues
use native default; explicit queue count must read back exactly. Preserve clone MAC.

Set every guest hardware/cloud-init baseline explicitly before boot. Firmware,
secure boot, SCSI layout, serial console, agent, ballooning, no auto-start and
no package upgrade remain enforced KDIVE requirements, not inherited defaults.
Remove guest dependence on the template FIXED dict. Include all guest configuration
choices in ownership identity while preserving the legacy default identity shape
where settings were already bound through the source (CPU host, same bridge/storage,
virtio and omitted queues). Changed settings must produce a distinct identity.

Template transport returns resolved source bridge/storage/VLAN internally. Validate
shape/types and recompute the bound source identity; strip private source fields
before public output. Native apply resolves under its existing source lock; guest
admission rechecks source identity and validates the guest destination network and
storage independently. Shared-template inventory comparisons exclude guest settings.

## Failure model

Reject foreign or drifted sources, malformed config/transport fields, unexpected
NIC properties, invalid CPU/model/queue values, unsupported network or storage,
source races, and guest drift. Keep strict SSH/TLS, locks, disk integrity checks,
partial-state rejection and read-only ready reruns. No template deletion/relabeling,
no guest auto-repair and no arbitrary native-option passthrough. Different source
image/firmware remains a different verified baseline. Existing caller-owned partial
guest cleanup uses the separately approved replacement operation.

## Verification

Focused red/green tests: existing tagged and untagged source reuse against different
guest settings; original markers/configs unchanged; malformed and foreign source
rejection; transport result binding/redaction; shared sources across guest settings;
CPU/model/queue validation; full native clone lifecycle with changed CPU, queue count,
bridge/storage/VLAN/sizing and cloud-init; drift rejection and legacy identity stability.
Guardrails: make check; independent full-diff review. Live: exact private lab inventory,
all four preserved templates and sequential fresh guest creation/verification on VLAN 12.

## Ballooning amendment

The operator explicitly requires the balloon driver enabled by default.
`balloon_mib` defaults to `memory_mib`, accepts 0 to disable or a target up to
configured memory. Set shares explicitly to the native default 1000. Admission
reserves maximum configured memory; verification bounds observed memory by the
balloon target and configured maximum, retaining bounded crash-reservation checks.
Require a bound virtio_balloon device when enabled. Explicit disabled ballooning
retains the legacy guest identity shape; enabled targets are identity-bound.
Test default enablement, disablement, lower target, invalid bounds/types, exact
native readback, missing driver and memory outside the configured range.

## Reviewed storage geometry

Clone admission budgets inherited auxiliary volumes rounded for both source and
destination. EFI readback retains that source bound; regenerated cloud-init media
uses only destination geometry. Reject root sizes below source-rounded allocation
before cloning. Regression proof covers both geometry directions and capacity one
byte short. Legacy disabled guests may omit shares, whose native default is 1000.
