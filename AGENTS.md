# Repository Guidelines

## Project Structure & Module Organization

This repository prepares Proxmox Linux VMs for KDIVE validation. Controller logic
and native host helpers live in `scripts/`; Ansible entry points are in
`playbooks/`, with reusable tasks in `roles/`. `inventory/example.yml` is the
anonymous inventory contract. `vars/` holds pinned image, kernel, and KDIVE
sources. Tests live in `tests/`; architecture decisions and design specifications
are under `docs/adr/` and `docs/workflow/specs/`. Consult `docs/` for lifecycle
procedures and prerequisites.

## Build, Test, and Development Commands

Use Python 3.12+ and the repository-pinned uv version on Linux or macOS.
Controllers may be ARM64 or x86_64; managed guests target x86_64.

- `make setup`: synchronize `.venv` from `uv.lock`.
- `make hooks`: install the shared pre-commit check.
- `make check`: run lint, Ansible syntax checks, inventory validation, and tests;
  this is also the Linux/macOS CI gate.
- `make lint`: check Ruff formatting, Python lint rules, and offline ansible-lint.
- `make test`: run the unittest suite without lab credentials.
- `make validate INVENTORY=inventory/private/lab.yml`: validate private inputs offline.

Live `make templates` and `make provision` require explicit `TARGETS` and default
to planning; review the plan before using `APPLY=1`. Follow `docs/` for restore,
teardown, snapshot levels, and live verification.

## Coding Style & Naming Conventions

Use four-space Python indentation, snake_case functions and modules, and
UPPER_CASE constants. Ruff enforces a 100-character line limit and import ordering;
format with `.venv/bin/ruff format scripts tests`. Use two-space YAML indentation,
descriptive task names, and fully qualified Ansible module names. Keep dependency
pins and `uv.lock` synchronized when changing dependencies.

## Testing Guidelines

Tests use standard-library `unittest`, with `test_*.py` files and `test_*` methods.
Cover invalid inputs, lifecycle ownership, failure paths, and output redaction;
mock external services rather than validation logic. No numeric coverage threshold
is configured. Offline checks do not establish live VM readiness: report separately
which authorized provisioning, verification, or restore paths were exercised.

## Commit & Pull Request Guidelines

History uses Conventional Commit prefixes such as `fix:`, `feat:`, `docs:`, and
`chore:`. Keep subjects imperative and changes focused. PR descriptions should
explain behavior, link relevant issues, and report checks and live-proof limits.
Update documentation when changing operator commands or configuration contracts.

## Security & Configuration

Keep private inventories under ignored `inventory/private/`; never commit `.env`,
credentials, SSH material, or private reports. Redact infrastructure identifiers
from shared evidence. Preserve TLS verification and strict SSH host-key checking.
