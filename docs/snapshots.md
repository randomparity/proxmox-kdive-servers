# Clean snapshot lifecycle

Fresh provisioning captures `clean` only after management preparation and baseline verification.
It gracefully shuts down, snapshots guest-writable root and EFI disks without RAM, boots, and
repeats strict SSH, OS/configuration, sizing, guest-agent, security-enforcement and KVM checks.
The cloud-init CD-ROM is guest-read-only: its volume reference and generating configuration are
bound, while native snapshotting skips its contents. Additional disks and unsupported storage
fail admission. The snapshot binds the existing image/guest identity and a stable native
configuration fingerprint; native generation-ID rotation on rollback is expected, while SMBIOS
identity remains bound. Output includes snapshot name, identity, configuration hash and timestamp.

Ordinary provision/verify reruns neither replace the snapshot nor reset current guest files.
Missing, stale, partial, RAM-state or mismatched snapshots fail. There is no command to capture
an already-used guest as clean. Do not manually rename another snapshot to `clean`: its metadata
will not satisfy the fresh baseline contract.

Restore and teardown default to read-only live plans. Select exact aliases; collect test results,
release external services/artifacts, and stop other consumers before applying. `EXCLUSIVE=1`
attests that release; cooperative locks serialize these tools but cannot fence a different
operator or application writing to the VM. KDIVE owns external state and result collection.

```sh
export INVENTORY=inventory/private/lab.yml
export TARGETS=ubuntu
unset APPLY CONFIRM EXCLUSIVE
make restore
# After reviewing the selected plan and releasing the consumer:
make restore APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1
```

Restore verifies the existing baseline before stopping a running guest, rolls back without RAM,
boots, and rechecks the full read-only baseline. It also accepts an already-stopped guest. The
`CONFIRM` value must match `TARGETS` exactly, including order; wildcards/empty/duplicate/unknown
aliases fail. A mismatch fails before API/SSH access.

Teardown permanently removes only selected owned VMs and their inspected snapshots/disks. It
preserves shared templates and unselected VMs. It accepts a complete matching older owned guest
without `clean`, enabling intentional replacement; absent selected guests are an unchanged result.
Foreign ownership, changed configuration, unknown disks or snapshot references, incomplete
allocations and native task locks require inspection instead of broader deletion.
Ubuntu teardown checks the seed reference but not its bytes. After the VM is gone it deletes
the seed only when every VM and container configuration in the cluster (current, pending and
snapshot sections) reads cleanly and none names it; otherwise the file is retained and the
command fails after removal so the operator can inspect it. A rerun reports the guest absent
and does not revisit the retained file.

```sh
make teardown
# This deletes the selected VM and its existing snapshots permanently:
make teardown APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1
# Recreate a verified fresh guest and clean baseline after reviewing SSH pins:
make provision
make provision APPLY=1
```

Recreation changes guest SSH keys. Preserve the private inventory/pin file, confirm the old
selected VM was removed, then remove only its obsolete address entry from that private
`known_hosts` using your trusted SSH administration process. The tools never replace pins or
disable host-key checking. Fresh first contact still requires a trusted lab network.

Equivalent separate playbooks retain protected private parsing/output:

```sh
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu \
  .venv/bin/ansible-playbook -i localhost, playbooks/restore.yml
INVENTORY=inventory/private/lab.yml TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1 \
  .venv/bin/ansible-playbook -i localhost, playbooks/teardown.yml
```

Commands await native task completion. Shutdown gets 180 seconds with no forced-stop fallback;
snapshot, rollback and deletion get 1800 seconds; startup SSH gets 600 seconds. An error or
transport timeout is not success or permission to retry: inspect native task/configuration state
privately first. A task may outlive its disconnected controller. Never clear its lock to bypass
inspection. Failed work retains its observed state; complete owned guests can be explicitly
torn down after tasks finish, while incomplete/ambiguous allocations need manual recovery.
Snapshots cover VM state only, not external services, DNS, backups or test artifact storage.

## Clean-only installation guests

KDIVE #2807 installation proofs need a guest that can always return to `clean`. On ZFS storage a
guest carrying `toolchain`, `kernel-src` or `kdive` snapshots cannot be restored to `clean`,
because newer snapshots block rollback (see [Operator-captured levels](levels.md#operator-captured-levels)),
and removing them would destroy the warm fixtures the levels exist to provide. Keep the two roles
on separate guests.

Give each host family (Ubuntu, Fedora, Rocky) a dedicated clean-only guest whose only snapshot
is `clean`, and never capture a higher level on it. The example inventory names them
`ubuntu-install`, `fedora-install` and `rocky-install`; they reuse the `ubuntu`, `fedora` and
`rocky` profiles and templates with their own VM IDs and network identities, so no level or
restore semantics change. The `ubuntu`, `fedora` and `rocky` aliases stay the full-chain
warm-fixture guests.

Use the pinned upstream runner through the controller orchestration target:

```sh
export TARGETS=fedora-install
export KDIVE_CHECKOUT=/path/to/clean-pinned-kdive
export KERNEL_BUNDLE=/path/to/upstream-kernel-bundle
export GUEST_IMAGE=fedora-kdive-ready-44
export OUTPUT=reports/install-proof-001
make kdive-install-proof
make kdive-install-proof APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1
```

`KDIVE_CHECKOUT` must be clean at the exact commit in `vars/kdive-source.json`, with its own
`.venv` synchronized using that checkout's instructions (Python 3.14). Prepare `KERNEL_BUNDLE`
from the pinned upstream checkout on its verified kernel-fixture host:

```sh
.venv/bin/python -m scripts.host_install_proof bundle \
  --fixture /path/to/verified-fixture --output /path/to/new-bundle
```

`GUEST_IMAGE` names an upstream catalog image matching the bundle architecture. This target
does not build the bundle, synchronize KDIVE dependencies or install prerequisites on the guest.
An optional `OPERATOR_PREREQUISITES=/path/to/script` passes an operator-owned UTF-8 script to
upstream's existing option; review its contents before apply. The wrapper records its digest and
supplies no default script. Missing prerequisites remain the runner's finding.

The plan checks the inputs, ownership and clean-only snapshot state without changing the guest.
Apply restores and verifies `clean`, runs the pinned controller-side runner over the inventory's
strict SSH path, then restores and verifies `clean` again. Upstream transfers its pinned source.
Existing lifecycle locks stay held through the sequence. Only one alias is accepted, and a guest
with any higher snapshot is refused on both ZFS and LVM-thin storage; no snapshots are deleted.
Ubuntu, Fedora and Rocky map to upstream debian, fedora and enterprise families. openSUSE is not
supported by the upstream runner.

Choose a new direct child of this checkout's ignored `reports/` directory for each run. The parent
and run directory must be private (mode 0700). `upstream/` retains upstream evidence unchanged;
`runner.log` contains private diagnostics. `provenance.json` separately binds the candidate,
controller revision, verified initial/final snapshot identity and configuration, runner exit,
reset verification, operator script digest and SHA-256 digests of retained upstream files.
Do not publish these files without redaction: they can identify private infrastructure.

Runner exits 0 (success), 1 (proof failure), 2 (invalid input) and 3 (blocked) are preserved.
The stdout summary reports `runner_exit_code` and `reset_verified` separately. The Python
entry point preserves the exit code; `make` maps any failed recipe to its own exit 2, so use
the summary when invoking the Make target. A reset failure
makes an otherwise successful invocation fail; it does not replace a nonzero runner status.
SIGINT/SIGTERM terminate the local runner process group before reset; timeout is 24 hours and
returns 124. Signals during reset are deferred. A failure of the initial verification skips the
runner but still attempts the final reset. A failed reset, lost transport, SIGKILL or power loss
requires operator inspection; no automatic retry can claim the guest is clean.

The dated live orchestration result is recorded in
[live proofs](live-proofs.md#clean-host-installation-orchestration).

For a fresh guest identity, use `make teardown` then `make provision` instead. Recreation changes
SSH keys; follow pin handling under [Clean snapshot lifecycle](#clean-snapshot-lifecycle).

The dedicated guests are additional VMs beyond the warm-fixture guests. Each uses the sizing in
[KDIVE installation validation sizing](inventory.md#kdive-installation-validation-sizing), so run them
sequentially, one exact target at a time, under the existing live admission checks. All seven
example aliases together would configure 56 vCPUs, 224 GiB RAM and 1.75 TiB of root disks, so
always set `TARGETS`; an unset value selects every alias.
