.PHONY: connect-opencode-global connect-opencode run-opencode env-opencode tailscale-on tailscale-off

connect-opencode-global: ## Enable default-on OpenCode tracing for this user
	@$(PYTHON) scripts/integrations/opencode_global.py

export PROJECT
connect-opencode: ## Enable the official OpenCode plugin for PROJECT=/path
	@$(PYTHON) scripts/integrations/opencode.py connect

run-opencode: ## Run OpenCode with a private server for PROJECT=/path
	@$(PYTHON) scripts/integrations/opencode.py run

env-opencode: ## Print the path of PROJECT's sourceable credentials file
	@$(PYTHON) scripts/integrations/opencode.py env

tailscale-on: ## Enable private Tailscale HTTPS access on dedicated ports
	@$(PYTHON) scripts/integrations/tailscale.py on

tailscale-off: ## Disable only this instance's Tailscale routes
	@$(PYTHON) scripts/integrations/tailscale.py off
