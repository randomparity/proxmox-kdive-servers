# Live proofs

Dated native evidence for the procedures in this directory. These are historical
observations of the recorded revisions and package versions, not current guarantees.

## Clean-host installation orchestration

Live orchestration proof on 2026-10-02 used Rocky Linux 10.2 on x86_64, controller
`f77b29186a3ea98e2be8a9ce16628ac661cbdf53`, and KDIVE candidate
`1321c285d02aadfde6e5842710521aaa0c2f6881`. The initial and final clean restores both passed
verification with matching snapshot identity. The upstream runner returned 1 after its
operator-prerequisites script could not start `docker.service`; installation and boot proof
were not reached. The wrapper retained 11 upstream evidence-file digests, reported the proof
failure separately from `reset_verified: true`, and completed in 236.550 seconds. This proves
the live failure-and-reset path; the successful-run path is covered by offline tests.
An independent `make verify LEVEL=clean` then passed in 31.753 seconds.

## Toolchain

Native proof on 2026-09-28: Ubuntu 24.04, Fedora 44 and openSUSE Leap 16.0 reached READY.
Their operator-captured snapshots passed verify and restore. Hiding `just` and removing the operator's `docker`
membership each produced an actionable verification failure; restoring the snapshot repaired
each fault and re-verified the level. AppArmor/SELinux baseline checks stayed enforcing. All three guests finished stopped with
`clean` and `toolchain` retained.

Rocky 10.2 native proof on 2026-09-28 used verifier commit
`1ac2316a5bc4274dff1f5848294b205858aed1c1`: clean restore → prepare → READY →
operator no-RAM `toolchain` capture → verify/restore passed. CRB supplied
`libvirt-devel 11.10.0-12.4.el10_2`; EPEL 10.2 supplied `ShellCheck 0.10.0-3.el10_0`,
bootstrapped by Rocky-signed `epel-release 10-7.el10_1`. Captured versions were
Docker 29.8.1, libvirt 11.10.0, uv 0.12.19 and just 1.58.0; shfmt's installed digest
and v3.14.1 version matched the approved binary. The initial live attempt exposed the
running-kernel module gap described in
[Toolchain preparation](levels.md#toolchain-preparation); the corrected clean-to-READY retry passed.

Renaming shfmt produced `Toolchain shfmt differs or unavailable; restore toolchain or re-prepare`.
Removing the operator's docker membership independently produced
`Toolchain operator missing docker group; restore toolchain or re-prepare`.
Each fault was followed by a successful snapshot restore and re-verification.
The final guest retained `clean` and `toolchain`, stopped with SELinux enforcing.
At that proof's conclusion, Rocky's `kernel-src` and `kdive` levels were unverified;
see the later LVM-thin preparation result below.

Measured snapshot allocation above `clean`:

| Profile | New blocks since clean (MiB) | Referenced-size increase (MiB) |
| --- | ---: | ---: |
| Ubuntu 24.04 | 820.29 | 786.87 |
| Fedora 44 | 1751.84 | 1667.27 |
| openSUSE Leap 16.0 | 1005.34 | 867.96 |
| Rocky 10.2 | 1593.07 | 1509.36 |

These are historical ZFS measurements: native ZFS `written@clean` and `referenced` differences at the captured toolchain
snapshot, summed across root and EFI volumes (1 MiB = 1,048,576 bytes). They exclude disk
reservations and are measurements of these package versions, not capacity guarantees.

## Kernel source

Native proof on 2026-09-28 used Linux `v6.9` commit
`a38297e3fb012ddfa7ce0321a7e5a8daeb1872b6` on Ubuntu 24.04 and Fedora 44.
Both reached READY and passed verification of the operator-captured snapshot. An
untracked file, ignored `.config`, wrong HEAD and wrong file owner each failed verification;
restoring `kernel-src` repaired each isolated fault and passed an independent reverify.
Both guests finished stopped with `clean`, `toolchain` and `kernel-src` retained.

Measured root-volume allocation above `toolchain` at READY:

| Profile | New blocks since toolchain (MiB) | Referenced-size increase (MiB) |
| --- | ---: | ---: |
| Ubuntu 24.04 | 934.66 | 785.68 |
| Fedora 44 | 705.05 | 537.04 |

These are historical ZFS measurements: native ZFS `written@toolchain` and `referenced` differences against the
parent snapshot (1 MiB = 1,048,576 bytes), excluding disk reservations. They measure
these prepared trees and guest writes, not a capacity guarantee.

## KDIVE installed level

Fedora 44 native proof used KDIVE commit
`dffce52ab48227ba7b8807824e1f79bb96dc18f6`. Upstream setup, host installation,
stack start and clean stop completed. A final systemd template-query defect was corrected;
the existing manifest and full parent chain were then audited through normal verification
and the unchanged native stop/READY handshake, without replaying installation. The operator
captured `kdive` without RAM. Ordinary verification and restore passed, followed by exactly
one passing HTTP authorization test with zero skips and strict stack revision checking.
No setup or Ansible step ran after restore. Final verification passed and the guest stopped.

Restore took 90.271 seconds; the test started 41.132 seconds after restore completed, or
131.402 seconds after restore began (controller-observed test-start marker). Historical ZFS measurement: native ZFS
allocation above `kernel-src` was 6,853.27 MiB written and 6,125.94 MiB additional referenced
data. These measurements include the explicitly preserved setup attempts and are not a
minimal clean-install size. The corrected final verifier was proven against that installation;
the entire fresh-install command was not replayed after the query-only correction.

Ubuntu 26.04 native proof on 2026-09-29 used KDIVE commit
`8182457399c25f1fd180806a10a0014d1877ed2b` on a fresh replacement guest provisioned with the
MAC-matched network seed. The operator captured `clean`, `toolchain`, `kernel-src` and
`kdive` without RAM after each READY; each level was verified and restored. The installed
lifecycle interpreters (the lifecycle service virtual environment and the KDIVE project
environment, both Python 3.14.4) import the native `guestfs` binding from Ubuntu's
`python3-guestfs`. After `kdive` restore, exactly one HTTP authorization test passed with zero
skips under strict stack revision checking; its only warning reported the Kubernetes-only
lifecycle witness as not deployed. No setup or Ansible step ran after restore. The stack
stopped with no containers or workers left, final verification passed and the guest stopped.

Restore took 95.071 seconds; the test started 39.316 seconds after restore completed, or
134.387 seconds after restore began (guest clock test-start marker). Historical ZFS measurement: native ZFS allocation
above `kernel-src` was 9,245.50 MiB written and 9,152.84 MiB additional referenced data for
a single clean installation.

## LVM-thin restore proof, 2026-10-02

Issue #41's live lower-to-higher restore proof passed on Rocky Linux 10.2 x86_64,
using Proxmox VE 9.2.20 with host kernel 7.0.14-17-pve and LVM-thin storage.
The controller was `cdcaaedee15a30ac6798f6fbab71090d9902ad92`; transmitted helpers
matched that committed source. KDIVE was the operator-approved
`81bd31a50d3c56e216d2e48dc10cf6196b41570e`, and kernel-source selection was `v6.9`.

The current guest's `clean` baseline passed verification. Missing `toolchain`
and `kernel-src` levels were prepared, captured without RAM, rebooted and verified.
Rocky prerequisites were Python 3.14.7-2.el10_2 with matching development headers
and libguestfs/runtime headers 1.58.1-9.el10_2 from official repositories; CRB was
enabled only for the prerequisite install command. KDIVE preparation then passed
runtime setup, the upstream host play, disposable local stack startup/shutdown,
ancestor checks, no-RAM capture and post-boot verification.

Both restore plans were reviewed before applying them with `TARGETS=rocky`,
`APPLY=1`, `CONFIRM=rocky` and `EXCLUSIVE=1`. Each restore was followed by a
separate verification invocation using the same private inventory:

| Operation | Result | Wall time |
| --- | --- | --- |
| `make level LEVEL=kdive CAPTURE=1` | Captured and verified | 406.043 s |
| `make restore LEVEL=clean` | Restored | 75.373 s |
| `make verify LEVEL=clean` | Passed | 31.160 s |
| `make restore LEVEL=kdive` | Restored | 102.167 s |
| `make verify LEVEL=kdive` | Passed | 48.235 s |

Independent native inspection confirmed the guest running on LVM-thin with
`clean`, `toolchain`, `kernel-src` and `kdive` snapshots retained, each without RAM
state. Final verification passed the installed KDIVE level and its ancestors;
workers and the disposable stack remained stopped as required for this fixture.
This proves the lower-to-higher path described in ADR0013. This run exercised no
HTTP authorization, nested workload or kdump test, and made no template or other
guest changes.

Earlier attempts rebuilt Fedora 44, openSUSE Leap 16.0, Rocky 10.2 and Ubuntu
26.04.1 LTS with two authorized keys and verified their lower snapshot chains.
Both public keys were checked in `authorized_keys`; only the login with an
available private key was exercised. Those are historical observations, not a
fresh verification of the other guests in this run.

Earlier Rocky preparation failures exposed a repository HTTP 503, missing native
prerequisites, missing system sbin lookup, root uv outside sudo's `secure_path`,
and developer setup replacing the ancestor's shfmt. The controller now supplies
explicit system lookup paths and calls runtime setup recipes without developer
tool installation. Focused regressions, a real Ansible/sudo probe and the live
capture/restore proof passed; source admission and ancestor guards stayed enabled.
