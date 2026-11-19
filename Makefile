.DEFAULT_GOAL := help

PYTHON ?= python3

include make/help.mk
include make/setup.mk
include make/stack.mk
include make/integrations.mk
include make/backup.mk

.PHONY: test lint check

test: ## Run the automated test suite
	$(PYTHON) -m unittest discover -s tests -v

lint: ## Check Python code with Ruff
	ruff check scripts tests
	ruff format --check scripts tests

check: lint test ## Run lint and automated tests
