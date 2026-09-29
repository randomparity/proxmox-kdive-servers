# KDIVE installed snapshot level

## Authority and scope

Issue #20 under epic #16; frozen charter q20-0a4bf761. The operator approved the
full proposal with Fedora fallback. Ubuntu's live Python3.14 lookup failed on
the existing24.04 guest; its proof is deferred to the operator/template owner.
Fedora44 is the first live target. openSUSE is excluded; Rocky needs its existing
toolchain gap resolved. No image migration, upstream installer changes, shared
services, snapshot replacement or warm guest-image/cache level is included.

## Contract

`make level LEVEL=kdive` extends the existing registry above kernel-src.
`vars/kdive-source.json` contains HTTPS repo and full lowercase40-hex commit;
initial pin is727de823e3ab1a00e7c5832e027a31155437d6f7. Inputs affect preparation
only; verify uses recorded metadata. The operator-owned checkout is src/kdive.
Existing dirty/mismatching trees and preexisting installed state are preserved
and refused. The generic snapshot envelope and transitive parent binding remain.
Content records repo, kdive_sha, kernel_commit and playbook_inputs_sha256; hash
only operator/source/kernel paths, fixed project identity, locality and ports.
Secrets and DSNs never appear in metadata or public command diagnostics.

Delegate `just setup`, the upstream local-libvirt host play, check-local-libvirt,
stack-services and stack-down. Do not substitute an installer. Command children
run as the operator in a fresh sanitized environment with explicit local Docker
socket and checkout identity. Bound total preparation to four hours and individual
setup/install/stack phases to two/one/half hours; controller/native timeouts cover
that bound. Failures preserve root-private logs and return only phase/remedy.
No ambient remote database, Docker context or Compose selection is inherited.

The level exclusively owns a fresh Compose project, refusing existing containers,
volumes or installer state before running installers. Compose's upstream env.sh
turns numeric port settings into wildcard publications, so a root-owned override
replaces all three backend port lists with127.0.0.1 bindings. Verify effective
configuration and actual running publications; volumes must have the exact
project labels, local driver and no remote driver options. The installer gets
only this guest's disposable witness-member DSN. No DSN input is exposed.
Stop through upstream stack-down without wipe/force; preserve named volumes for
snapshot restore. Root-state ownership/modes and override bytes are verified.

Preparation checks the fresh-login libvirt contract, installed lifecycle socket
enabled state and service template load state, clean checkout and kernel identity.
The existing run_level ancestor checks run after installation; a changed critical
uv/just/Docker/libvirt version fails instead of blessing the changed parent.
READY is emitted only after all checks and graceful guest shutdown. Snapshot
capture remains external, with exact READY metadata supplied to the operator.

## Failure model

Deployment is an exclusive disposable guest, controlled by its trusted operator
and authenticated controller. Untrusted inventory/metadata crosses validation;
upstream pinned code executes with installer privileges under explicit approval.
A malicious root/operator able to replace executable code is outside this model.
Wrong pin, dirty tree, unsupported distro/interpreter, existing state, unrelated
Docker resources, unsafe publication/storage, installer/stop timeout, missing
units and stale parent fail closed without snapshot capture or destructive reset.
A partial install remains visible for operator disposition; retries never erase it.
Network/package availability and upstream installer failures may block this level.
No compatibility patch or ancestor version upgrade is inferred from such failure.

## Validation

Focused tests exercise inputs, source mismatch, clean refusal, private errors,
local-only backend admission, exact volumes, unit checks, stop failure, READY
ordering and parent/transitive invalidation. Tests inject controlled faults to
prove rejection, mocking subprocess/SSH boundaries rather than validator logic.
Run make check, iterative independent review/security pass, Linux/macOS CI.
Live Fedora: verify kernel-src, prepare, READY, operator no-RAM capture, verify,
restore, start existing stack without installation, source its local environment
and run the single real HTTP authorization test named in the approved plan.
Require exactly one pass and no skips; record coherent revision and elapsed
restore-to-proof-start time. This is not a VM-provisioning or kdump proof.
Stop the stack and guest afterward. Preserve existing snapshots and all ancestors.

Restored operations use the same explicit clean environment as preparation: local Docker socket,
COMPOSE_PROJECT_NAME=kdive-level and COMPOSE_FILE pointing to the checkout compose plus the
root-owned override. README supplies the exact invocation; source env.sh only inside it.
Check effective and running backend locality before the proof. Upstream stop can return success
while host daemons remain, so observe upstream daemon_pids and all eight worker units inactive;
a surviving daemon or worker blocks READY even when stack-down exits0. The lifecycle socket
remains enabled. Validate the installed root-owned0600 witness file against the fixed local
member DSN, without printing it; shell programs carrying credentials travel on stdin, not argv.
