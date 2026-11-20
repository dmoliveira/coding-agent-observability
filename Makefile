.DEFAULT_GOAL := help

PYTHON ?= python3

include make/help.mk
include make/setup.mk
include make/stack.mk
include make/integrations.mk
include make/backup.mk

.PHONY: test lint check prepare-opencode-test prepare-site site-build smoke-opencode-global smoke-opencode-shared

prepare-opencode-test: ## Install and prepare the pinned OpenCode test plugin
	npm --prefix integrations/opencode-global ci --ignore-scripts --no-audit --no-fund
	npm --prefix integrations/opencode-global run prepare-instrumentation

prepare-site:
	npm --prefix site ci --ignore-scripts --no-audit --no-fund

site-build: prepare-site ## Build the public documentation site into site/dist
	npm --prefix site run build

test: prepare-opencode-test prepare-site ## Run the automated test suite
	$(PYTHON) -m unittest discover -s tests -v
	npm --prefix integrations/opencode-global test
	npm --prefix site test

lint: ## Check Python code with Ruff
	ruff check scripts tests
	ruff format --check scripts tests

check: lint test ## Run lint and automated tests

smoke-opencode-global: prepare-opencode-test ## Test OpenCode tracing using local synthetic services
	$(PYTHON) tests/smoke_opencode_global.py

smoke-opencode-shared: prepare-opencode-test ## Test shared-server tracing through an in-flight reload
	$(PYTHON) tests/smoke_opencode_shared.py
