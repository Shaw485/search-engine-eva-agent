PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
QUERY ?= wireless mouse

.PHONY: help setup format lint policy check test smoke data-sample api \
	opensearch-up opensearch-down smoke-opensearch eval-baseline web clean

help:
	@echo "setup             Create the virtual environment and install dependencies"
	@echo "format            Format Python source with Ruff"
	@echo "lint              Check Python formatting and lint rules"
	@echo "policy            Reject large data, local secrets, and private keys"
	@echo "check             Run lint, repository policy, tests, and local smoke"
	@echo "test              Run the Stage 0 test suite"
	@echo "smoke             Run deterministic local BM25 and vector smoke search"
	@echo "data-sample       Validate the 10-product smoke fixture"
	@echo "api               Start the local FastAPI service"
	@echo "opensearch-up     Start the optional local OpenSearch service"
	@echo "smoke-opensearch  Run the same smoke contract against OpenSearch"
	@echo "opensearch-down   Stop the optional OpenSearch service"

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements-dev.lock
	$(PIP) install --no-build-isolation --no-deps -e .

format:
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

lint:
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/ruff check .

policy:
	$(VENV_PYTHON) scripts/check_repository_policy.py

check: lint policy test smoke

test:
	$(VENV_PYTHON) -m pytest

smoke:
	@$(VENV_PYTHON) -m search_quality.smoke --backend local --query "$(QUERY)"

data-sample:
	$(VENV_PYTHON) -m search_quality.sample_data data/samples/products.json

api:
	$(VENV_PYTHON) -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000

opensearch-up:
	docker compose up -d opensearch

smoke-opensearch:
	@OPENSEARCH_ALLOW_INDEX_RESET=true $(VENV_PYTHON) -m search_quality.smoke \
		--backend opensearch --query "$(QUERY)"

opensearch-down:
	docker compose down

eval-baseline:
	@echo "Stage 2 command reserved: ranking metrics are not implemented in Stage 0." >&2
	@exit 2

web:
	@echo "Stage 7 command reserved: the Web product is not implemented in Stage 0." >&2
	@exit 2

clean:
	rm -rf .pytest_cache build dist src/*.egg-info
