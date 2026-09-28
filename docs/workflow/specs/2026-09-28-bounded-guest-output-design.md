# Bounded guest SSH responses

## Problem and authority

Issue #9 requests receive-time bounds for guest RPC and readiness SSH output.
Both currently use full-output capture, so post-read validation comes too late.
The approved scope is the guest transport and its tests; snapshot lifecycle (#5),
KDIVE operations (KDIVE owners), and host-side transport (separate follow-up owner)
remain excluded. Scope token: q9-6c194b20.

## Design

Use one internal `run_guest(argv, *, input=None, timeout)` operation for both
call sites. It returns `subprocess.CompletedProcess` with decoded string streams.
Existing SSH options, identity checks, caller timeouts and protocol validation stay
with their present owners. The duplicated full-output captures are removed.

Use standard-library selectors over nonblocking stdout, stderr and optional stdin.
Send request bytes in bounded chunks while draining both output pipes so input
backpressure cannot prevent observing output overflow. Closing stdin on completion
or broken pipe preserves subprocess communication behavior. No input is sent for
readiness probes. Read at most the remaining shared budget plus one byte, capped
at 4096 bytes per read. Reject before extending either buffer when stdout plus
stderr exceeds 65536 bytes. Both streams therefore have a receive-time bound;
exactly 65536 combined bytes remains admissible. Decode UTF-8 strictly only after
bounded capture and normal process exit; malformed encoding raises a sanitized
validation error. Existing JSON and boot-identity validation still follows.

One monotonic deadline covers sending, receiving and waiting for normal exit.
Expiration raises `TimeoutExpired` for the existing caller's sanitized error.
On abnormal completion close all pipe descriptors, kill the local transport if
still running, and wait at most five seconds to reap it. Use no Popen context
manager: its implicit unbounded wait would defeat cleanup's deadline. Overflow
raises a fixed `ValidationError` without remote bytes or command details.

Overflow is failure even for reboot RPC exit 255 and readiness exit zero. Only
bounded, successfully decoded output may reach the existing reboot-disconnect
exception; no overflow can initiate another reboot or promote a ready guest.

## Failure model

Deployments are the repository's Linux and macOS Python controllers using local
OpenSSH to authenticated managed guests. Actors include verbose/faulty guest
commands and ordinary local transport failures. Assets are bounded controller
response memory, transport lifecycle, sanitized diagnostics and existing guest
identity/reboot correctness. Cover output floods on either/both streams, blocked
stdin, timeout, early EOF, partial JSON, bad encoding and child exit before EOF.
Controller OOM, scheduler starvation and uninterruptible kernel processes cannot
be made to terminate by Python; cleanup waits remain bounded and surface failure.
Host transport policy and hostile guest-root containment are outside this repair.

## Success and validation

Real child tests exercise both migrated callers plus stream multiplexing directly:
stdout, stderr and combined excess fail; exactly bounded valid JSON succeeds;
malformed UTF-8 and partial JSON fail without echoing output; blocked input and
closed-output/live-child timeouts terminate and reap; output overflow beats blocked
input and never takes reboot-disconnect or readiness-success paths. Existing
exchange tests preserve one reboot and post-boot identity proof. New tests first
fail against the old transport, then pass. Run `make check` locally and the existing
Linux/macOS CI arms. No live reboot or package transaction is required by #9.
