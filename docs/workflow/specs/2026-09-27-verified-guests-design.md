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

The existing `vlan` field specifies the guest NIC independently of the source VLAN.
Read only the selected source's original VLAN from authenticated API configuration,
reconstruct its immutable template request and require its ready identity to match.
Native admission repeats complete source identity/configuration/disk verification;
API/native disagreement fails before writes. Bridge, storage and pinned image inputs
remain shared and unchanged. Template-only commands retain their existing immutable
VLAN behavior. No source-VLAN inventory field is added.

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
Explicitly configure the clone NIC's desired tag (or no tag), preserving its generated
MAC address. Bind guest VLAN separately into the guest baseline digest, and admit
the guest's tagged-network prerequisite even when its source is untagged.
Verify managed disks and native snapshot capability for the future #5 consumer.
Before first start, replace the owned clone's generated IDE cloud-init seed with
a generated seed on `scsi1`, using its existing VirtIO SCSI controller. The pinned
Fedora guest did not discover the IDE CD-ROM despite valid host-side `cidata` media;
SCSI effectiveness remains a required live-proof arm. This frees and creates only
the small generated seed volume; it never removes root or EFI volumes. Validate
the stopped preparing clone, exact seed ownership/type/size and all managed disks
before removal. Use native configuration digests, read back each step, and require
all other configuration, including UUID/root/EFI references, to remain identical.
Keep source-template IDE configuration and its verification unchanged. A failed
replacement leaves a stopped inspectable partial; ordinary commands never resume it.

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
The private prepared event carries the owned native VM's SMBIOS UUID. Before
preparation, the guest must match that UUID from read-only DMI data, together with
the expected OS/release/architecture, hostname/FQDN and static IPv4. Normalize UUID
case through the standard UUID parser; missing, malformed or mismatched identity
fails before package, service, module or file changes. Exclude the UUID from public
summaries. This admission binds a reachable SSH peer to the selected owned clone.

Await authenticated SSH and successful cloud-init before preparation. Install
only missing SSH/sudo/Python/cloud-init/guest-agent prerequisites with native
package tools, without general upgrade. The pinned images already supply the
first four; Ubuntu alone lacks the agent. Record actual management package
versions. Enable/start the guest agent for fresh guests only. Load the installed
vendor KVM module and persist its name for boot. Missing modules fail, except for
the explicitly approved pinned openSUSE prerequisite below. Never fall back to
TCG or weaken existing security enforcement.

### Pinned openSUSE kernel prerequisite

The operator approved replacing `kernel-default-base`
`6.12.0-160000.38.1.160000.2.24` with `kernel-default` `6.12.0-160000.38.1`
and `ucode-intel` `20260812-160000.1.1`, all x86_64, only during fresh preparation.
The pinned minimal image lacks KVM modules; the full package contains signed
modules with matching vermagic and an identical kernel binary. This exception
includes normal vendor package scriptlets and one graceful guest reboot, not a
general upgrade or a template/image change. Record both prerequisite versions.

Admit the exact running kernel/base package, enforcing security and sufficient
guest free space before downloading. Use the configured vendor repository with
metadata signature checking and native downloads into private temporary storage.
Check both downloaded RPM identities and signatures. The actual native install
process must present exactly two pinned installs and the pinned base removal;
validate its XML solver summary before answering its one confirmation prompt.
The guest provides a private controlling terminal because zypper reads replies
from `/dev/tty`; XML output stays on a separate pipe, and no noninteractive
default-answer mode is used.
Reject extra/different packages, action types, trust prompts, repeated prompts,
malformed or oversized output, errors and timeouts. Do not trust an earlier dry-run
as authority for a later unchecked transaction. Verify the resulting package set,
kernel build/binary and preserved security before accepting preparation.

Extend the existing readiness exchange with one internal reboot phase only for a
fresh openSUSE guest. After authenticated preparation and baseline verification,
the native session rechecks owned preparing configuration and permits the single
reboot phase while retaining its locks. The controller strictly authenticates the
guest, rechecks its native-bound UUID and previous boot ID, and issues exactly one
`systemctl --no-block reboot` request. No force/reset option or retry is used.
This avoids native reboot operations that can apply pending VM configuration.
Only SSH exit 255 with empty stdout during that reboot request is treated as an
ambiguous disconnect. It does not acknowledge success: proceed only to the strict
post-boot proof below, without issuing another reboot. Other failures and invalid
or partial responses stop for inspection.

Reconnect with the existing SSH pin, require the same UUID and a different valid
boot ID, and run the full common verifier with preparation disabled. Only its
correlated acknowledgement allows ready marking. Boot identity remains private.
Acknowledgements carry the expected internal phase: `prepared` for initial
verification and `post-reboot` for the second openSUSE verification. A missing,
wrong or replayed first-phase acknowledgement cannot promote the guest to ready.
Ready reruns and all other profiles never enter this reboot phase. A second or
unexpected reboot event is rejected, and failure retains the owned partial.

The first native acknowledgement allows 2500 seconds: SSH readiness at most 600,
guest preparation/verification at most 1800, plus transport margin. The second
allows 2700: reboot request at most 60, strict changed-boot reconnect at most 600,
read-only verification at most 1800, plus margin. Native event reads remain bounded
at 2400 seconds; no single native reboot task is introduced. Guest package downloads
and interactive installation have their own bounded time/output budgets inside
the overall 1800-second preparation envelope. Expiry never initiates another reboot
or readiness promotion.

Verify distro ID/release and x86_64, short hostname and FQDN, selected static IPv4,
successful cloud-init, authenticated account with noninteractive sudo, exact CPU
count, accounted memory at least 90% of configured MiB, and root filesystem capacity at
least configured disk bytes minus max(2 GiB, 10%). Native disks must separately
match configured size. Require active guest agent and native ping; SELinux
enforcing on Fedora/Rocky/openSUSE, enabled AppArmor with enforcing profiles on
Ubuntu. The approved openSUSE prerequisite is the only kernel package exception.

Report usable `/proc/meminfo` MemTotal as `memory_bytes`, separately from the
measured `/sys/kernel/kexec_crash_size` reservation as `crash_reserved_bytes`.
Only their sum is used for allocation proof. The pinned Rocky kernel reserves
256 MiB at 4 GiB, which MemTotal excludes. Its installed default range tops out
at 512 MiB; cap accepted reservation there and at one quarter of configured RAM
so small guests cannot compensate for missing memory with an excessive reserve.
Both measurements must be nonnegative integers (usable RAM positive) and their
sum cannot exceed configured RAM. Missing sysfs support means zero reservation;
other read failures or malformed values fail. Preserve kernel and kdump settings.
This follows the [kernel crash reservation semantics](https://docs.kernel.org/admin-guide/kdump/kdump.html)
and the [vendor reservation ranges](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/managing_monitoring_and_updating_the_kernel/supported-kdump-configurations-and-targets_assembly_managing-kernel-command-line-parameters-with-uki).

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

Successful bootstrap permits only the verified Proxmox scalar-`user` deprecation:
cloud-init status `done`, no true errors, exit 0 or 2, and every recoverable notice
exactly the advisory naming deprecation in 22.2 and removal in 27.2. Exit 2 without
that advisory and all mixed/unknown notices fail. Preserve warnings in guest logs;
this compatibility classification neither repairs the generator nor weakens identity,
security or KVM checks. Native generator format changes remain host lifecycle work.

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
