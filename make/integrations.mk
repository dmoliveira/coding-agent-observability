.PHONY: connect-opencode-global connect-opencode-events disconnect-opencode-events connect-opencode run-opencode env-opencode tailscale-on tailscale-off lmstudio-models connect-lmstudio sync-lmstudio-models span-dashboard span-dashboard-demo agent-dashboards agent-report agent-eval-demo

connect-opencode-global: ## Enable default-on OpenCode tracing for this user
	@$(PYTHON) scripts/integrations/opencode_global.py

connect-opencode-events: ## Enable separate structural execution/compaction events
	@$(PYTHON) scripts/integrations/opencode_events.py connect

disconnect-opencode-events: ## Disable only the owned structural event plugin
	@$(PYTHON) scripts/integrations/opencode_events.py disconnect

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

lmstudio-models: ## List downloaded LM Studio models and loaded status
	@$(PYTHON) scripts/integrations/lmstudio.py models

connect-lmstudio: ## Connect local LM Studio and add its available language models
	@$(PYTHON) scripts/integrations/lmstudio.py connect

sync-lmstudio-models: ## Add newly available LM Studio models to the managed connection
	@$(PYTHON) scripts/integrations/lmstudio.py sync

span-dashboard: ## Create or update the local span-classification dashboard
	@$(PYTHON) scripts/integrations/span_dashboard.py apply

span-dashboard-demo: ## Send three explicitly marked synthetic spans for classification
	@$(PYTHON) scripts/integrations/span_dashboard.py demo

agent-dashboards: ## Create or update measured OpenCode and project-tool dashboards
	@$(PYTHON) scripts/integrations/agent_dashboard.py

agent-report: ## Print a bounded metadata-only OpenCode report as JSON
	@$(PYTHON) scripts/integrations/agent_report.py

agent-eval-demo: ## Judge three synthetic task examples using local LM Studio
	@$(PYTHON) scripts/integrations/agent_evaluate.py --demo
