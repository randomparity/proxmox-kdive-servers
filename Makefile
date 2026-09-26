export PATH := $(CURDIR)/.venv/bin:$(PATH)

INVENTORY ?= inventory/example.yml
export INVENTORY
ifneq ($(origin TARGETS), undefined)
export TARGETS
endif

.PHONY: setup hooks lint syntax validate test check

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

validate:
	.venv/bin/python scripts/validate_inventory.py

test:
	.venv/bin/python -m unittest discover -s tests -v

check: lint syntax validate test
