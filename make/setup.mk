.PHONY: doctor setup

doctor: ## Check local prerequisites without changing anything
	@$(PYTHON) scripts/setup/doctor.py

setup: ## Create private first-run configuration; never overwrite existing secrets
	@$(PYTHON) scripts/setup/initialize.py
