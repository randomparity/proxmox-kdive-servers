# Proxmox KDIVE server infrastructure

This repository prepares clean Linux VMs for
[KDIVE validation](https://github.com/randomparity/kdive/issues/2803).
It provides offline inventory checks, verified Proxmox templates, and full-clone
guests with verified nested KVM and fresh clean snapshots for four Linux families.
Selected restore and teardown reset managed VM state; KDIVE installation remains separate.

Optional snapshot levels layer on `clean` in a fixed order: `toolchain` (build tools, Docker,
libvirt), `kernel-src` (a pinned Linux checkout) and `kdive` (a pinned KDIVE installation).
Every live command is a read-only plan until you pass `APPLY=1`; restore, teardown, level
preparation and installation proofs also need `CONFIRM` and `EXCLUSIVE`.

## Quick start: build all four VMs

This workflow creates Ubuntu, Fedora, Rocky Linux and openSUSE templates, then
provisions and verifies one guest from each for KDIVE testing. Complete
[controller setup](#controller-setup) first and check the Proxmox host, storage,
API permissions and SSH prerequisites in [Verified templates](docs/templates.md).
The host must also have working nested KVM before guest provisioning.

Create a private inventory if you do not already have one:

```sh
mkdir -p inventory/private
chmod 700 inventory/private
cp -n inventory/example.yml inventory/private/lab.yml
chmod 600 inventory/private/lab.yml
```

Edit `inventory/private/lab.yml` using the [input reference](docs/inventory.md).
Replace the example endpoints, node/storage/bridge/VLAN, guest account/public key,
FQDNs, static addresses, gateway and DNS servers with your assigned values. Assign
unused guest and template VM IDs and confirm CPU/RAM/disk sizing. Keep the aliases
`ubuntu`, `fedora`, `rocky` and `opensuse` for the commands below.

Select all four and validate the inventory offline before contacting Proxmox:

```sh
export INVENTORY=inventory/private/lab.yml
export TARGETS=ubuntu,fedora,rocky,opensuse
unset APPLY RESUME
make validate
```

`make validate` checks both guest and template inputs, including types, required
fields, address consistency and global uniqueness. It does not require credentials
or prove that the live host has the requested resources.

Export the credentials named by the inventory's three `api_*_env` fields. If you
keep them in a trusted, ignored `.env` file, load it with `set -a; . ./.env; set +a`.
Configure trusted API TLS and Proxmox SSH host keys as described in
[Verified templates](docs/templates.md).

Plan template creation, review the result, then apply it:

```sh
make templates
# After reviewing the plan:
make templates APPLY=1
```

With all templates ready, plan and create the four guests:

```sh
make provision
# After reviewing the plan:
make provision APPLY=1
make verify
```

Plans perform live admission checks; apply repeats admission before creation.
Provisioning verifies each guest's boot, networking, sizing, security enforcement
and nested-KVM baseline, captures a no-RAM `clean` snapshot, then boots and verifies again.
`make verify` repeats verification without provisioning.
These commands prepare the VMs; KDIVE installation is a separate step.

**Capacity:** the example requests 32 vCPUs, 128 GiB RAM and 1 TiB of guest root
disks, plus host headroom, templates and auxiliary disks. When new guests' vCPUs or
RAM exceed the host's observed free capacity, the plan and the apply print a
`Guest capacity warning` on stderr naming the node, the request and the observed
capacity, then proceed: these lab guests are not expected to be fully loaded at the
same time. Storage must still fit. See
[sizing guidance](docs/inventory.md#kdive-installation-validation-sizing).

## Controller setup

Use a macOS or Linux controller with Git, Make and
[uv 0.12.19](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.12 or newer is required by the pinned Ansible release; `.python-version`
selects Python 3.12 and uv installs it if needed. The controller may be ARM64 or
x86_64. Managed guest targets are **x86_64**, independently of the controller.

```sh
make setup
make check
make hooks
```

`make setup` creates `.venv` using `uv.lock`, including exact versions of
Ansible Core (2.21.4), ansible-lint (26.9.0), Ruff (0.16.9), and pre-commit (4.6.2).
Versions were checked against the package registry on 2026-09-26. Setup needs
network access to download tools; subsequent checks need no lab access or credentials.
`make hooks` installs the repository's local pre-commit hook. The hook and both
Linux/macOS CI jobs call the same `make check` recipe. The hook uses the anonymous
example even when your shell selects a private inventory.

Individual checks are `make lint`, `make syntax`, `make validate`, and `make test`.
Run Python commands through `.venv/bin/python` to use the installed controller.
Update dependency pins intentionally, regenerate `uv.lock` with `uv lock`, then
run the shared checks. The lock includes transitive dependencies and hashes.

## Documentation

| Topic | Contents |
| --- | --- |
| [Private inventory](docs/inventory.md) | Inventory fields, validation rules, sizing guidance |
| [Verified templates](docs/templates.md) | Proxmox host prerequisites, storage choice, template import |
| [Verified guests](docs/guests.md) | Provisioning, verification checks, KDIVE handoff |
| [Clean snapshot lifecycle](docs/snapshots.md) | Restore, teardown, clean-only installation proofs |
| [Operator-captured levels](docs/levels.md) | `toolchain`, `kernel-src` and `kdive` levels |
| [Live proofs](docs/live-proofs.md) | Dated native evidence and measured snapshot sizes |
| [Decisions](docs/adr/) | Architecture decision records |
| [Designs](docs/workflow/specs/) | Design specifications behind the decisions |
