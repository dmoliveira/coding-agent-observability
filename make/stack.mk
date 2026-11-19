.PHONY: up down status logs open verify

up: ## Start the local Langfuse stack
	@$(PYTHON) scripts/stack/manage.py up

down: ## Stop the stack and preserve all data volumes
	@$(PYTHON) scripts/stack/manage.py down

status: ## Show container status
	@$(PYTHON) scripts/stack/manage.py status

verify: ## Check server health, authentication and loopback bindings
	@$(PYTHON) scripts/stack/verify.py

logs: ## Follow Langfuse logs (which may contain sensitive information)
	@$(PYTHON) scripts/stack/manage.py logs

open: ## Open Langfuse in your default browser
	@$(PYTHON) scripts/stack/manage.py open
