# Verified guests

Complete guest inputs, including each unique `fqdn`, before provisioning. The
short first DNS label becomes the guest hostname; the remainder becomes its DNS
search domain. For example, `ubuntu.example.invalid` identifies a guest
independently of its static `ansible_host`. External DNS records remain operator
owned. Guest validation also checks template inputs and guest/template ID collisions.

Guest provisioning reads the selected source's original VLAN through the authenticated
API and revalidates its exact immutable identity and configuration through native SSH.
The guest's requested VLAN is bound separately and applied while preserving the cloned
NIC's MAC address. Tagged and untagged guests can share a source template without
changing that template. The API token therefore needs selected-template configuration
audit access as well as node/storage visibility. Template operations resolve the same original source settings, so the same guest
inventory works for both template checks and provisioning.

Keep the private inventory directory mode 0700. Provisioning creates its
`known_hosts` file mode 0600 for guest SSH pins. Fresh owned clones use OpenSSH
`accept-new` once on the operator-trusted lab network; privileged preparation and
subsequent access use strict verification. This assumes trustworthy first contact.
Stored keys are never removed/replaced: a mismatch requires operator inspection.

Cloud-init must finish with no errors. One exact compatibility advisory is accepted:
Proxmox 9.2 generates a scalar `user` value, which cloud-init deprecates until its
scheduled removal in 27.2. Status must be `done`; every recoverable notice must be
that advisory. All other notices and failures are rejected. This explains the
documented [cloud-init exit code 2](https://docs.cloud-init.io/en/latest/explanation/return_codes.html)
seen with the pinned Fedora image; it does not change the host generator or suppress
guest logging. Template/host lifecycle owners must address the format before removal.
Private keys remain on the controller. Optional `ansible_ssh_private_key_file`
selects the guest identity; otherwise ordinary OpenSSH identities apply.
Inventory paths may contain spaces. Literal `${`, line breaks and NUL are rejected
before allocation to prevent OpenSSH from reinterpreting the pin location.

```sh
# Export API credentials as described in docs/templates.md, then inspect the read-only plan:
make provision INVENTORY=inventory/private/lab.yml TARGETS=ubuntu
# Create only the selected absent, owned full clone and verify its baseline:
make provision INVENTORY=inventory/private/lab.yml TARGETS=ubuntu APPLY=1
# Reusable read-only verification, including after a separately managed restore:
make verify INVENTORY=inventory/private/lab.yml TARGETS=ubuntu
```

Use comma-separated exact aliases for multiple instances. All selected batches
are planned before an apply; admission repeats under the native allocation lock.
Full root/auxiliary disks must fit reported storage. When new cores exceed host
logical CPUs minus the rounded-up maximum of observed busy CPUs and one-minute load,
or new RAM exceeds `MemAvailable`, admission prints a capacity warning per native
host to stderr and proceeds. Apply prints it for the pre-apply plan and again for
the locked recheck. These are observations, not dedicated-core reservations: the
operator controls concurrent workloads. The Ansible entrypoints hide controller
output under `no_log` but print these warning lines from a successful run; on failure
Ansible censors the result, so run the `make` plan to see them. Missing storage
capacity, missing or invalid CPU/memory metrics, nesting, source ownership or native
evidence fails before allocation. The host is never reconfigured or rebooted, and
there is no TCG fallback.

Native cooperating locks serialize allocations and selected VM/template operations
through readiness. Full clones receive exact hardware/static networking, optional
VLAN, NIC model/queues, keys, selected CPU model, guest agent and balloon device,
with no automatic startup or general cloud-init package upgrade. Root filesystem growth is verified after boot.
Guest configuration is applied explicitly before boot: template defaults do not
select the guest CPU model, NIC, destination bridge/storage or cloud-init values.
The CPU model must expose working nested KVM; there is no emulation fallback.

Ballooning is enabled by default with target equal to configured RAM. Set
`balloon_mib` lower to allow reclaiming memory, or set it to 0 to disable the device.
Admission still reserves maximum configured RAM; ballooning does not authorize
memory overcommit. Verification requires a bound guest `virtio_balloon` driver when
enabled and checks observed RAM against the configured balloon target and maximum.
NIC queues use the native default when omitted; explicit `nic_queues` is verified
exactly and requires `nic_model: virtio`. NIC model names follow Proxmox's supported
models, including `virtio`, `e1000`, `e1000e` and `vmxnet3`.
Before first start, provisioning replaces only the clone's generated IDE cloud-init
seed with a generated `scsi1` seed on its VirtIO SCSI controller. Native removal
frees the old seed volume; creation regenerates it from inventory. Root and EFI
volumes, VM UUID and source templates are preserved and checked. Failure leaves
an inspectable partial; reruns do not resume or repair it.
Ubuntu clones first receive an immutable network seed `kdive-net-<vmid>-<sha256>.yaml` in
the snippet storage, referenced as `cicustom: network=<storage>:snippets/<name>`. Its name is
the SHA-256 of its bytes, and the bytes carry the guest identity, so the file is never
rewritten; an existing file is reused only when byte-identical, root owned, singly linked
and not group- or world-writable. Provisioning, verify, level preparation, restore and every
start re-derive the seed from the NIC MAC and inventory and require the exact reference and
bytes; a missing or changed seed stops before start for operator inspection.
Only fresh clones receive management preparation: the pinned Ubuntu image needs
`qemu-guest-agent`; the other three already contain it. Existing SSH/sudo/Python/
cloud-init are checked. The installed guest KVM vendor module is loaded and named
in `/etc/modules-load.d/kdive-kvm.conf` for reboot readiness.
The pinned openSUSE Minimal-VM image contains `kernel-default-base` without KVM
modules. Fresh openSUSE provisioning replaces that exact base package
`6.12.0-160000.38.1.160000.2.24` with signed `kernel-default 6.12.0-160000.38.1`
and `ucode-intel 20260812-160000.1.1`. It verifies the two RPM identities/signatures
and the actual native transaction before its sole confirmation. No other package
action is accepted. Normal vendor RPM scriptlets update guest boot artifacts;
the running kernel build and kernel binary must remain unchanged. SELinux and
integrity lockdown stay enforced. Other missing vendor KVM modules fail.
After initial verification, fresh openSUSE receives one graceful authenticated
`systemctl --no-block reboot` request. Strict reconnect must prove the same VM
UUID, changed boot identity and full baseline again before ready marking. A
failure retains the partial. An SSH disconnect during the one reboot request may
carry no response or the complete reboot acknowledgement; both require the same
strict post-boot proof. Invalid responses stop for inspection.
There is no forced power fallback, reboot retry or automatic recovery. Ready reruns remain read-only.
`clean` provisioning installs no libvirt, KDIVE, build/debug tooling or runners.

Verification checks successful cloud-init, authenticated SSH/noninteractive sudo,
actual OS/release/x86_64, hostname/FQDN/static IPv4, CPU/RAM/root filesystem, active
guest agent plus native ping, and enforcing SELinux or AppArmor. It opens the KVM
API as root, requires version 12 and creates/closes a transient VM descriptor.
This proves capability; KDIVE owns later virtualization permissions and workloads.
Before any guest preparation, read-only checks match the guest DMI UUID to the
owned VM's native SMBIOS UUID and verify its OS and network identity. A stale IP
cannot authorize preparation merely by accepting the configured SSH credential.
The UUID and boot identity travel privately and are excluded from public evidence.
RAM evidence reports usable `memory_bytes` and measured `crash_reserved_bytes`
separately. Allocation proof requires their sum to be at least 90% of configured
RAM (or the positive balloon target), without changing the image's crash-kernel
settings. Only the native sysfs reservation counts, capped at 512 MiB and one quarter of configured RAM; their
sum cannot exceed maximum configured RAM. Ubuntu guests allow up to 1 GiB (still at most one
quarter of RAM) because the Ubuntu kdump tools installed by KDIVE reserve 1 GiB from 32 GiB. Missing crash-reservation support counts as
zero; malformed or unreadable evidence fails.

Matching ready reruns perform verification only: they do not restart a stopped
guest, update packages/keys, resize disks or clear test state. Drift, extra disks,
pending native changes, foreign ownership and preparing-phase objects fail for
inspection. Failed work retains inspectable resources; there is no automatic
delete, resume or repair. Ready does not mean current test state is clean. A missing or
stale `clean` snapshot fails rather than blessing a used VM; explicitly selected teardown
and fresh provisioning is the recreation path.

The equivalent Ansible entrypoints use the same controller operation:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/provision.yml
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/verify.yml
```

Add `APPLY=1` only for provisioning. Keep `-i localhost,`; private parsing and task
output remain protected. Make emits sanitized identities, observed baseline and
duration JSON without account names, endpoints, addresses or keys.

For KDIVE handoff, supply the private ordinary inventory and guest `known_hosts`
through a private channel. Point its SSH/Ansible `UserKnownHostsFile` at that file;
keep host-key checking enabled, use its normal guest account/identity and guest
Python rather than the controller virtualenv. Assign one exclusive test consumer
per VM. The consumer must release the VM before provisioning verification or later
restore/teardown, and collect external test results/artifacts before release.
Coordinate this explicitly; this repository does not supply a scheduler. Keep
private inventory and SSH pins after worktree cleanup because they identify access
to persistent VMs. Sanitized reports may be shared; raw native/guest logs may not.
