# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Controller-side tooling that builds verified Proxmox templates and full-clone x86_64 guests
(Ubuntu, Fedora, Rocky, openSUSE) for KDIVE validation, with snapshot "levels" layered on a
`clean` baseline. It does not install KDIVE itself outside the `kdive` level. `README.md` is the
operator contract; `docs/adr/` records accepted decisions and `docs/workflow/specs/` the designs
behind them.

## Commands

Tooling is pinned via uv (`required-version = 0.12.19`) and `uv.lock`; always run tools from
`.venv/bin` (the Makefile prepends it to `PATH`).

```sh
make setup      # uv sync --locked
make hooks      # install the pre-commit hook (runs `make check`)
make check      # lint + syntax + validate + test — the same gate CI runs on Linux and macOS
make lint       # ruff check + ruff format --check (scripts, tests) + ansible-lint --offline
make syntax     # ansible-playbook --syntax-check on every playbook
make validate   # offline inventory validation (guest and --purpose templates)
make test       # python -m unittest discover -s tests -v
```

Single test (tests import `scripts.*` as a package, so run from the repo root):

```sh
.venv/bin/python -m unittest tests.test_guests -v
.venv/bin/python -m unittest tests.test_guests.TestGuestHost.<test_name> -v
```

Live operations (need a private inventory, credentials and Proxmox access) are plan-by-default;
mutation requires explicit flags:

```sh
make templates|provision|verify|level|restore|teardown   # read-only plan / check
make provision APPLY=1
make restore  APPLY=1 CONFIRM="$TARGETS" EXCLUSIVE=1     # destructive ops also need CONFIRM+EXCLUSIVE
make level LEVEL=toolchain TARGETS=ubuntu APPLY=1 CONFIRM=ubuntu EXCLUSIVE=1
```

Environment knobs: `INVENTORY` (default `inventory/example.yml`), `TARGETS` (comma-separated
aliases; omitted means all only for read-only validation), `APPLY`, `RESUME`, `CONFIRM`,
`EXCLUSIVE`, `LEVEL` (default `clean`), `CAPTURE` (default off; `1` on `make level` captures,
boots and verifies after preparation). Offline checks must always pass against the anonymous
`inventory/example.yml`; the hook unsets `INVENTORY`/`TARGETS`.

## Architecture

The logic lives in stdlib-only Python under `scripts/`; the Ansible playbooks and the two roles
are thin wrappers that invoke those scripts with `no_log: true` and derive `changed` from the
scripts' JSON-lines stdout (`action` field). Changes to behavior go in the scripts, not YAML.

Code runs in three places, and source is shipped rather than installed:

- **Controller** — `validate_inventory.py` (inventory contract, shared `require`/`ValidationError`),
  `templates.py` and `guests.py` (select hosts, build requests, drive the remote helpers,
  validate every event they emit, run guest SSH).
- **Proxmox host** — `template_host.py` and `guest_host.py` run as root over SSH via
  `python3 -c`. `guests.host_ssh` bundles `validate_inventory`, `template_host` and `guest_verify`
  into a compressed bootstrap that registers them as modules before executing `guest_host.py`.
  These files therefore must stay stdlib-only and must not rely on files on the host.
- **Guest** — `guest_verify.py` is sent over SSH as `sudo -n python3 -c` and receives a JSON
  envelope on stdin. It holds the baseline checks and the ordered level registry (`LEVELS`:
  `clean` → `toolchain` → `kernel-src` → `kdive`), each with `prepare` and `check` functions.

Scripts support both `python scripts/x.py` and package import (`if __package__:` import blocks);
keep both branches in sync when adding imports.

Cross-cutting contracts (see the ADRs before changing them):

- Host → controller is a line-oriented JSON event protocol with phases (`planned`, `prepared`,
  `ready`, `levels`, `snapshot-ready`, …); `guests.validate_event` checks exact key sets per phase.
- Host error text is never shown to the operator. The host emits allow-listed reason codes and the
  controller maps them to its own messages (`HOST_REASONS` in `guests.py`, ADR 0015). Capacity
  oversubscription is a stderr `Guest capacity warning:` line, not a failure (ADR 0014).
- Ownership is marked in the Proxmox VM description (`kdive-template-v1:<identity>:<phase>`,
  `kdive-guest-v1:<identity>:<phase>`). Reruns verify only; drift or partial state is refused
  for operator inspection, never repaired or replaced automatically (ADR 0003).
- Level snapshots default to operator capture with emitted name and metadata (ADR 0007).
  Opt-in `CAPTURE=1` / `--capture` on level preparation captures, boots and verifies under the
  existing APPLY/CONFIRM/EXCLUSIVE gates (ADR 0016); failures retain state without retries or
  snapshot replacement/deletion. `lvmthin` storage allows restoring any level (ADR 0013).
- Image pins and inspected baselines live in `vars/images.json`; kernel and KDIVE source pins in
  `vars/kernel-source.json` and `vars/kdive-source.json`.

Tests mock the host/guest boundary (subprocess, SSH, native Proxmox commands; see
`NativeFixture` in `tests/test_templates.py`) and exercise the real logic, including the shipped
modules. They need no lab access.

## Repository conventions

- New accepted decisions get the next numbered ADR in `docs/adr/`; designs go in
  `docs/workflow/specs/YYYY-MM-DD-<topic>-design.md`.
- Ruff: line length 100, rules `E,F,I,B,UP`, target py312.
- Never commit anything under `inventory/` except `example.yml`, nor `.env*`, `reports/`, `.agent/`
  or key material; the ignore rules are not a secret scanner.
