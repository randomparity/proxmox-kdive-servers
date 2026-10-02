# KDIVE installed-level compatibility

## Problem and scope

Issue #41's approved live run exposed two invocation conflicts: `just setup`
installs developer shfmt over the retained toolchain, and the privileged host
play cannot find its root-installed uv under Rocky's sudo secure_path.
The operator approved fixing both on `feat/lvmthin-live-proof-41`.
Scope is the controller invocation, focused regressions and operator guidance.
ADR0010 retains upstream installation ownership; ADR0013/0016 retain restore
and capture policy. The approved source pin and ancestor contracts remain intact.

## Design and success

Replace developer setup with existing upstream recipes: `just sync`,
`just build-capture-bootstrap-manifest`, then `just install-ansible-collections`.
Use separate fail-fast shell lines. Do not install developer tools or Git hooks.
Existing toolchain and documented native prerequisites supply host dependencies;
upstream's host play continues to install runtime packages and services.

Add a literal `ansible_become_flags` play input retaining `-H -S -n` and supplying
`PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin`.
This sets only the invoked command environment; it changes no sudoers file.
No inherited PATH, home directory or user-writable executable path enters root
lookup. Pass through the existing JSON extra-vars and shell quoting boundary.
The upstream checkout remains unchanged and its source admission remains strict.
Keep the existing snapshot configuration hash unchanged: this execution-only
lookup correction must not invalidate previously recorded installed levels.

Success: setup uses runtime recipes without changing shfmt; failed recipes stop
before host installation; generated extra-vars deliver the fixed PATH to a real
Ansible privileged command with unrelated task environment retained. Existing
source, failure censorship and ancestor-verification tests continue to pass.
Live proof still requires an explicitly approved source matching upstream main,
committed/transmitted helper identity, planned capture and restores, independent
verification and retained no-RAM snapshots. Do not claim live success from mocks.

## Failure model

- Actors/deployments: trusted Linux/macOS ARM64 or x86_64 controller operators;
  managed x86_64 guests, with this live proof bounded to the released Rocky guest.
- Invariants/assets: immutable ancestor tools, approved source, private errors,
  fixed privileged lookup and untouched sudo policy; other guests and templates.
- Accepted failures: missing native prerequisites, unavailable upstream services
  or a moved upstream main stop preparation visibly; no automatic retry is added.
- Covered elsewhere: existing confirmation/exclusive gates and snapshot ownership;
  restore redesign remains excluded and separately owned if a defect is exposed.

## Threat model

Ansible extra-vars cross the privilege boundary. A literal system-only PATH and
existing JSON/shell encoding exclude inherited executable injection. Root-owned
system directories and the approved upstream installer remain trusted. No new
configuration key, guard bypass, credential publication or package pin is added.

## Validation

Focused tests execute the generated setup script against executable boundary
fixtures, including an earlier recipe failure, and inspect decoded host inputs.
A local real Ansible/sudo probe validates PATH propagation without VM mutation.
Run KDIVE tests and make lint, then mandatory commit-hook make check. README
historical claims are compared with private evidence, not tested as prose.
