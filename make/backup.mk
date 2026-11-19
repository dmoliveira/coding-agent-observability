.PHONY: backup restore

backup: ## Export a stopped instance and its secrets to a private archive
	@$(PYTHON) scripts/backup/manage.py backup

export BACKUP
restore: ## Import BACKUP=/path into a new, empty installation
	@$(PYTHON) scripts/backup/manage.py restore
