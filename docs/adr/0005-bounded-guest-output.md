# ADR 0005: Bound guest response capture with selectors

## Status

Accepted for issue #9 under scope q9-6c194b20.

## Context

Guest RPC and readiness calls capture unbounded SSH output before validating it.
The repair must bound both streams while preserving request input, timeouts and
reboot semantics on Linux and macOS controllers.

## Decision

Replace both captures with one internal selector-driven subprocess operation.
Multiplex input and output, admit at most 65536 combined output bytes, then decode.
A monotonic deadline covers communication and exit. Abnormal completion closes
pipes, kills the local transport and attempts reaping with a five-second timeout.

## Consequences

The cap counts bytes across both streams, including reboot diagnostics. UTF-8
failures and overflow produce fixed sanitized errors before caller policy runs.
No dependency or platform-specific command is added. Kernel-level nontermination
can outlast the bounded reaping attempt and is outside application guarantees.

## Considered & rejected

- Full capture followed by validation — fact: the existing subprocess invocation
  with a harmless Python child writing 131073 bytes returns all 131073 before
  caller validation; it cannot enforce a receive-time bound.
- Reader threads — judgment: concurrent readers need additional ownership and
  shutdown coordination; selectors already serve the supported POSIX controllers.
- Redirect streams to disk — judgment: shifts unbounded output to storage and adds
  temporary-file lifecycle without satisfying incremental receive rejection.
