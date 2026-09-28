export PATH := $(CURDIR)/.venv/bin:$(PATH)

INVENTORY ?= inventory/example.yml
export INVENTORY
ifneq ($(origin APPLY), undefined)
export APPLY
endif
ifneq ($(origin RESUME), undefined)
export RESUME
endif
ifneq ($(origin TARGETS), undefined)
export TARGETS
endif

LEVEL ?= clean
export CONFIRM EXCLUSIVE LEVEL

.PHONY: setup hooks lint syntax validate templates provision verify restore teardown level test check

setup:
	uv sync --locked

hooks:
	.venv/bin/pre-commit install

lint:
	.venv/bin/ruff check scripts tests
	.venv/bin/ruff format --check scripts tests
	.venv/bin/ansible-lint --offline

syntax:
	.venv/bin/ansible-playbook -i localhost, playbooks/validate.yml --syntax-check
	.venv/bin/ansible-playbook -i localhost, playbooks/templates.yml --syntax-check
	.venv/bin/ansible-playbook -i localhost, playbooks/provision.yml --syntax-check
	.venv/bin/ansible-playbook -i localhost, playbooks/verify.yml --syntax-check
	.venv/bin/ansible-playbook -i localhost, playbooks/restore.yml --syntax-check
	.venv/bin/ansible-playbook -i localhost, playbooks/teardown.yml --syntax-check

validate:
	.venv/bin/python scripts/validate_inventory.py
	.venv/bin/python scripts/validate_inventory.py --purpose templates

templates:
	.venv/bin/python scripts/templates.py

provision:
	.venv/bin/python scripts/guests.py

verify:
	.venv/bin/python scripts/guests.py --verify

level:
	.venv/bin/python scripts/guests.py --prepare-level

restore:
	.venv/bin/python scripts/guests.py --restore

teardown:
	.venv/bin/python scripts/guests.py --teardown

test:
	.venv/bin/python -m unittest discover -s tests -v

check: lint syntax validate test
