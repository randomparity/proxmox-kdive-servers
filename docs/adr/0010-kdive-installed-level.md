# ADR0010: Delegate the KDIVE installed level to the pinned upstream installer

## Status

Accepted by operator approval of issue20's full proposal with Fedora fallback.

## Context

The existing level protocol supplies ownership and parent binding. Issue20 needs
an installed local-libvirt stack whose captured witness cannot affect shared data.
Upstream retains the installation and stack lifecycle contracts.

## Decision

Add kdive above kernel-src, with explicit checkout revision and non-secret play
input hash. Delegate setup/host play/lifecycle; use only a fresh guest-local
Compose backend project, loopback publications and local volumes. Preserve those
volumes on stop. Refuse partial/preexisting installations rather than resetting
unknown state. Capture remains outside product code.

Ubuntu24.04 lacks an installed Python3.14 interpreter in the approved live probe.
The operator approved Fedora-first proof and deferred Ubuntu to the template
owner; this does not claim Ubuntu compatibility or authorize an image migration.

## Considered and rejected

- [judgment] Keep installing every time: fails the requested reusable level.
- [fact] Trust upstream default publication: docker-compose.yml uses loopback
  defaults, but scripts/live-stack/env.sh exports numeric backend ports, which
  override those defaults; enforce explicit loopback port replacement instead.
- [judgment] Accept an external witness DSN: cannot guarantee a disposable snapshot.
- [judgment] Reimplement the installer or build Ubuntu bindings: outside approved
  ownership; pin and invoke upstream, report unsupported configurations.

## Consequences

Installation changes packages/services and may fail on upstream prerequisites.
Capture is withheld on any failure, critical ancestor drift or unsafe backend.
The restored bounded HTTP proof establishes a running installed stack, not a full
nested VM or kdump workflow. Existing parent checks retain their original meaning.
