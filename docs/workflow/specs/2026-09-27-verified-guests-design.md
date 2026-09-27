# Verified guest provisioning

## Authority and outcome

Issue #4, parent #1, and scope token `q4-c1e9047b` require explicitly selected
full clones of the four pinned x86_64 profiles with authenticated management
access and actual nested KVM. The operator approved the DNS identity and
first-contact SSH trust design. Live execution needs separate authorization.
[ADR 0003](../../adr/0003-verified-guest-lifecycle.md) records the lifecycle choice.

## Global constraints

Use the existing locked controller dependencies and Python 3.12+ on macOS/Linux,
ARM64/x86_64. Native Proxmox operations require the existing x86_64 Linux host,
root SSH, Python 3.11+, qm/pvesh/qemu-img and supported zfspool/lvmthin storage.
Guest verification uses the images' existing Python 3 and Linux interfaces.
Add no dependencies. Keep real inventory, keys, addresses and reports private.
No host reconfiguration, kernel replacement, security downgrade, KDIVE tooling,
snapshots, rollback, teardown, scheduler or automatic deletion/replacement.

## Interfaces and ownership

Ordinary inventory remains the sole input. Add required unique lowercase `fqdn`
(DNS labels, maximum 253 characters, at least two labels); `ansible_host` remains
the static IPv4 endpoint. Guest provisioning also requires valid existing template
inputs. Validate all managed identities and guest/template collisions before selection.
An optional `ansible_ssh_private_key_file` uses the existing inventory convention.

`make provision INVENTORY=... TARGETS=...` is a read-only live plan. `APPLY=1`
permits fresh allocation/preparation only. `make verify` performs read-only baseline
checks for explicit selected ready guests. Controller-only Ansible playbooks expose
these same commands with protected task output. No new inventory format or scheduler.

`scripts/guests.py` owns controller validation, API admission, transport, selected
operation ordering and private SSH host-key pinning. `scripts/guest_host.py` owns
native ownership/admission/clone/configuration/locking and readiness marking.
`scripts/guest_verify.py` owns the reusable guest readiness contract and narrowly
bounded fresh preparation. Existing template identity and native helpers remain
their current owner's responsibility. No existing public entry point is removed.

## Admission and serialization

Inspect selected templates with the existing exact identity/configuration/disk
verification. Query authoritative cluster-wide resources: errors do not mean absent.
Validate root Linux/x86_64, local node identity, storage/bridge/VLAN, host nesting
enabled, and usable host KVM before allocation. Require requested disks at least
the source virtual size. Exact selected aliases are mandatory; patterns fail.

CPU demand is the sum of new selected guests' cores. Admit only when that demand
plus `ceil(max(cpu_fraction * logical_cpus, load1))` fits logical CPUs. Require
finite CPU fraction in [0,1], finite nonnegative load1 and positive integer CPUs.
Memory demand must fit native `MemAvailable`; disk demand includes rounded root
and bounded auxiliary allocations on each storage. Missing metrics fail closed.
This is point-in-time admission, not dedicated cores or a workload guarantee.
The operator owns external workload concurrency and resource changes.

Apply uses the existing native nonblocking lock primitive: reserved lock ID 0
serializes cooperating guest allocations on the host, selected VM IDs guard their
lifecycle, and source template IDs guard clones against template operations.
Keep the existing lock namespace for compatibility. Hold locks through readiness,
recheck admission under them and await synchronous qm completion. Plan and verify
do not create locks or alter native resources; concurrent drift causes failure.

## Fresh lifecycle and reruns

Derive a versioned baseline digest from the selected source template identity,
guest configuration and management-baseline version. Full clone with its intended
name and ownership marker in the clone operation; verify the resulting owned clone
before further changes. Configure CPU host, exact cores/RAM, balloon disabled,
static network/VLAN/resolvers, short name plus search domain, cloud-init account
and keys, guest agent enabled, `ciupgrade=0`, and `onboot=0`; grow the root disk.
Verify managed disks and native snapshot capability for the future #5 consumer.

The marker has `preparing` and `ready` phases. It binds the same configuration
identity across provisioning and future snapshot verification. A fresh run may
prepare only its newly cloned owned VM. Mark ready only after guest checks and
native guest-agent ping succeed. Prepared events, acknowledgements and results bind
the current selected VMID and baseline digest; mismatched, duplicate or out-of-order
acknowledgements fail without ready marking. Failed/interrupted preparation leaves its phase
visible and no cleanup; ordinary reruns reject partial objects for inspection.
There is no implicit resume, repair, shutdown or destructive recreate operation.

A matching ready rerun verifies configuration and running guest state without
changing keys/packages/configuration, rebooting, restarting or clearing test data.
Meaningful drift and pending native changes fail with the affected contract and
inspection guidance. Ownership mismatch, extra writable disks and foreign VMIDs
fail before changes. No claim that a ready VM still contains untouched test state:
#5 owns clean-snapshot identity and safe restoration.

## Guest access, preparation and verification

For a newly cloned owned VM only, the approved trusted-lab first contact uses
OpenSSH `accept-new` with a private controller known_hosts file. Subsequent SSH
uses strict checking; existing stored keys are never removed or replaced. Pinning
does not authenticate the first network peer against a malicious lab network.
Host SSH continues its existing strict key policy. Do not copy private keys to
the host or guest. Use bounded connection/boot/command deadlines.

Await authenticated SSH and successful cloud-init before preparation. Install
only missing SSH/sudo/Python/cloud-init/guest-agent prerequisites with native
package tools, without general upgrade. The pinned images already supply the
first four; Ubuntu alone lacks the agent. Record actual management package
versions. Enable/start the guest agent for fresh guests only. Load the installed
vendor KVM module and persist its name for boot; missing modules fail rather than
installing a kernel or falling back to TCG. Preserve existing security enforcement.

Verify distro ID/release and x86_64, short hostname and FQDN, selected static IPv4,
successful cloud-init, authenticated account with noninteractive sudo, exact CPU
count, memory at least 90% of configured MiB, and root filesystem capacity at
least configured disk bytes minus max(2 GiB, 10%). Native disks must separately
match configured size. Require active guest agent and native ping; SELinux
enforcing on Fedora/Rocky/openSUSE, enabled AppArmor with enforcing profiles on
Ubuntu. Existing image packages/modules must support these checks unchanged.

Open `/dev/kvm` as root, require KVM_GET_API_VERSION=12, call KVM_CREATE_VM and
immediately close its descriptor. This is capability proof, not a nested workload.
The verifier is also callable independently after #5 restores a baseline.

## Evidence and handoff

Emit sanitized JSON per selected profile: action, source/baseline/configuration
digests, revision, OS/release/architecture, observed sizing/security/KVM, package
versions and elapsed seconds. Exclude endpoint names, addresses, keys and account
identifiers. Failures report operation/contract and next inspection action without
native stdout/stderr. Preserve unfinished allocation instead of hiding failure.

Document private inventory handoff to KDIVE, one exclusive consumer per VM,
release before lifecycle operations and collection of external test results.
Ansible inventory remains directly usable; controller Python is not guest Python.

## Failure model

- Actors/deployment: trusted operator and controller, authenticated existing
  Proxmox host, pinned vendor images, four Linux guest profiles, cooperating
  commands and operator-controlled lab networking. Package mirrors are external.
- Invariants/assets: selected-only mutation; foreign VMs and shared templates
  unchanged; no resetting test data on reruns; stable source/baseline identity;
  private credentials and host identity; enforcing guest security; actual KVM.
- Accepted failures: external concurrent workload changes can invalidate capacity
  after admission; commands fail rather than promise reservation. Interruptions
  may retain owned partial VMs needing inspection. First-contact SSH assumes the
  operator-approved trusted network. An authenticated malicious root host is
  outside the control boundary; it already owns guest execution and disks.
- Other owners: operator handles host/network/DNS and out-of-band resource edits;
  #5 handles clean snapshots, restore, teardown and automated exclusive-use
  coordination; KDIVE handles installation/tests and external state collection.

## Threat model

- Added boundaries: ordinary inventory to native guest requests; host result to
  controller; controller to guest SSH; guest verifier output to public summaries.
  Existing template/API/SSH boundaries are reused, not replaced.
- Actors: trusted local operator supplies inventory/keys; remote services and
  malformed responses can fail. Another cooperating invocation may contend.
  First-contact lab-peer substitution is the explicitly accepted trust decision.
- Controls: strict type/range/selection/identity validation, argv/JSON encoding,
  bounded reads/deadlines, private key-pin file with strict subsequent checks,
  ownership before mutation, native locks and sanitized allowlisted results.
- Out of scope: hostile authenticated hypervisor/root controller and external
  operator races are privileged deployment assumptions, not new isolation claims.

## Validation

Focused tests cover FQDN validation and global identity collisions; explicit
selection; capacity boundaries/missing metrics; absent versus failed resource
reads; template ownership and source drift; clone/configure/readiness ordering;
partial failure and ready-rerun immutability; pin preservation; sanitized errors;
guest OS/hostname/sizing/security/KVM checks and management-only preparation.
Mock subprocess/native interfaces, retaining real decision logic.

Run shared `make check` on the assembled candidate. After separate operator
authorization, live-create all four selected profiles and retain measured proof.
Change one selected VM's `onboot` 0 to 1 without reboot/data changes; prove rerun
rejects and leaves drift intact, restore exact original value, verify again.
Compare an unselected VM's native configuration digest across that scoped run.
No live proof means no completed issue/merge-ready claim.
