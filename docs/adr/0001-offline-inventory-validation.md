# ADR 0001: Validate one ordinary Ansible inventory offline

## Status

Accepted by the operator on 2026-09-26 for issue #2.

## Context

The approved bootstrap needs credential-free input checks and ordinary inventory
for later image, provisioning and reset work. No executable implementation exists.

## Decision

Keep static YAML inventory as the only configuration source. Resolve inheritance
with Ansible's built-in YAML inventory plugin, then validate the resolved values
using Python's standard library. A controller-local playbook calls the same script.
Use explicit aliases for selection and environment-variable names for credentials;
validation never resolves credentials. Static IPv4 is the initial network contract.
Use Python 3.12+ and uv-locked, exactly pinned controller tooling.

## Consequences

Local hooks and CI exercise one offline boundary. Tests need no live lab or secret.
Configuration validity cannot establish live capacity, ownership, image integrity,
or readiness; #3–#5 add those proofs. Additional network families require a later
explicit contract change. Ansible inventory remains trusted local configuration.

## Considered & rejected

- **Pure Ansible assertions.** judgment: networking, type boundaries and negative
  selection tests are clearer in the standard library than in long Jinja expressions.
- **A second JSON/YAML schema and generator.** judgment: duplicates inventory and
  adds a translation boundary without an existing consumer needing it.
- **Defer validation until provisioning.** judgment: fails #2's required offline
  controller proof and delays private-input errors until live work.
