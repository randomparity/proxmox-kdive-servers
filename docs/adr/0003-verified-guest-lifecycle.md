# ADR 0003: Verify owned guest baselines without repairing reruns

## Status

Accepted by the operator's issue #4 design approval on 2026-09-27.

Hardware-default coupling is superseded by [ADR 0004](0004-independent-guest-configuration.md).

## Context

The four verified templates need individually selected full clones, actual
nested KVM, private authenticated access and an identity reusable after rollback.
Existing ordinary inventory and native template ownership already define inputs.
Test use may change guest state; provisioning must not silently reset it.

## Decision

Extend ordinary inventory with unique guest `fqdn`, separate from SSH IPv4.
Retain native template identity and create owned, full-cloned guest resources.
Resolve the source's original VLAN from authenticated configuration and fully recheck
its identity before mutation. Bind and configure the ordinary inventory guest VLAN
independently, preserving the clone MAC and immutable source template.
Use a versioned configuration digest and preparing/ready marker. Only a fresh
owned clone receives management preparation. A ready rerun only verifies; drift
and partial state require operator inspection, never automatic replacement.
Hold native cooperating locks through readiness and keep shared template locks.

Use existing controller Python/native tools with a common guest verifier.
First-contact guest SSH key pinning is authorized on the trusted lab network;
subsequent access is strict and never replaces a stored key. Disable general
cloud-init package upgrades and preserve guest security policy. Loading the guest
KVM module is a prerequisite, not host reconfiguration. The operator separately
approved one pinned openSUSE exception: replace its minimal kernel package with
the matching full vendor package and resolved microcode package, then gracefully
reboot that fresh guest once before readiness. Preserve the kernel build, source
template and all other guests. Exact package/signature/transaction checks and
strict same-UUID, changed-boot-identity verification bound this exception; failure
retains a preparing partial and never authorizes another reboot or automatic repair.

## Consequences

First-contact peer trust depends on the approved lab network. Root-capable
guest access proves KVM; KDIVE owns later unprivileged virtualization setup.
Interrupted runs retain inspectable resources. #5 can reuse baseline identity
and verification without ordinary provisioning blessing test-modified state.
Point-in-time capacity admission does not reserve dedicated physical cores.

## Considered & rejected

- **Automatic reconciliation/recreation.** judgment: it can reset test state
  and confuses provisioning with the separately scoped snapshot lifecycle.
- **Manual initial SSH key enrollment.** judgment: stronger first-contact
  assurance, but the operator selected private first-contact pinning for this lab.
- **Cloud-init snippets and template customization.** verified: installed
  Proxmox 9.2 Cloudinit.pm generates short hostname/FQDN from name/searchdomain;
  native configuration supplies this contract without extra storage or image edits.
- **Another provisioning collection or scheduler.** judgment: native synchronous
  commands and the existing inventory provide the required bounded lifecycle.
