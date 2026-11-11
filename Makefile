.PHONY: help install dev-ha-up dev-ha-down dev-ha-logs dev-seed dev-simulate run test lint fmt check inspect docker-build mcp-up mcp-down mcp-logs

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

check: lint test ## Lint + tests (run before every commit)

inspect: ## Open MCP Inspector against the stdio server
	npx -y @modelcontextprotocol/inspector uv run ha-insights-mcp

docker-build: ## Build the HTTP server image (ha-insights-mcp:dev)
	docker build -t ha-insights-mcp:dev .

mcp-up: ## Run the HTTP server in Docker next to the dev HA (needs tokens.toml)
	@test -f tokens.toml || (echo "tokens.toml missing: see tokens.example.toml" && exit 1)
	@test -f policy.toml || cp policy.example.toml policy.toml
	docker compose --profile mcp up -d --build ha-insights
	@echo "MCP: http://localhost:$${MCP_HOST_PORT:-8000}/mcp"

mcp-down: ## Stop the HTTP server container
	docker compose --profile mcp rm -sf ha-insights

mcp-logs: ## Follow the HTTP server logs
	docker compose --profile mcp logs -f ha-insights
