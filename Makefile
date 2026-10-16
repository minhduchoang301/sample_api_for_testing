SHELL := /bin/bash
UV := env -u VIRTUAL_ENV uv
BUGS ?=

.PHONY: up down logs lint verify build

up: ## Build and start checkout + provider (BUGS=B01,B02 to switch defects on)
	BUGS="$(BUGS)" docker compose up -d --build --wait

down: ## Stop the stack and delete the database volume
	docker compose down -v

logs: ## Follow logs from both services
	docker compose logs -f

build:
	docker compose build

lint: ## ruff + mypy
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

verify: ## Prove baseline behaviour and every seeded defect (needs Docker)
	$(UV) run playwright install chromium
	$(UV) run python scripts/verify_defects.py $(ARGS)
