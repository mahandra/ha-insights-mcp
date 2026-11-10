.PHONY: help install dev-ha-up dev-ha-down dev-ha-logs dev-seed dev-simulate run test lint fmt inspect

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	uv sync

dev-ha-up: ## Start the dev Home Assistant (demo integration) on :8123
	@test -f dev/ha-config/automations.yaml || echo "[]" > dev/ha-config/automations.yaml
	docker compose up -d
	@echo "Home Assistant: http://localhost:8123 (first start takes ~1 min)"

dev-ha-down: ## Stop the dev Home Assistant
	docker compose down

dev-ha-logs: ## Tail dev Home Assistant logs
	docker compose logs -f homeassistant

dev-seed: ## Create demo areas and assign demo devices (dev HA only)
	uv run python dev/seed_areas.py

dev-simulate: ## Inject problems, history and automations into the dev HA
	uv run python dev/simulate.py

run: ## Run the MCP server over stdio
	uv run ha-insights-mcp

test: ## Run unit tests (no real HA calls)
	uv run pytest -q

lint: ## Lint with ruff
	uv run ruff check src tests dev
	uv run ruff format --check src tests dev

fmt: ## Auto-format with ruff
	uv run ruff check --fix src tests dev
	uv run ruff format src tests dev

inspect: ## Open MCP Inspector against the stdio server
	npx -y @modelcontextprotocol/inspector uv run ha-insights-mcp
