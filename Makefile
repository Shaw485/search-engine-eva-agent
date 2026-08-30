PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
QUERY ?= wireless mouse
EVAL_PROFILE ?= smoke
EVAL_RANKER ?= all

.PHONY: help setup format lint policy check test smoke data-sample data-download \
	data-esci-validate data-esci-build api opensearch-up opensearch-down \
	smoke-opensearch eval-baseline compare-runs agent-smoke agent-eval \
	query-set-smoke bad-cases-smoke catalog-index agent-api-volcengine-macos \
	web clean

help:
	@echo "setup             Create the virtual environment and install dependencies"
	@echo "format            Format Python source with Ruff"
	@echo "lint              Check Python formatting and lint rules"
	@echo "policy            Reject large data, local secrets, and private keys"
	@echo "check             Run lint, repository policy, tests, and local smoke"
	@echo "test              Run the full unit and contract test suite"
	@echo "smoke             Run deterministic local BM25 and vector smoke search"
	@echo "data-sample       Validate the 10-product smoke fixture"
	@echo "data-download     Download and verify the pinned Amazon ESCI sources"
	@echo "data-esci-validate Verify source sizes, hashes, magic and schemas"
	@echo "data-esci-build   Build deterministic Stage 1 train/dev/test assets"
	@echo "catalog-index     Build the 1,814,924-product SQLite FTS5 index"
	@echo "api               Start the local FastAPI service"
	@echo "agent-api-volcengine-macos Experimental Agent Plan launcher; check usage-policy gate first"
	@echo "opensearch-up     Start the optional local OpenSearch service"
	@echo "smoke-opensearch  Run the same smoke contract against OpenSearch"
	@echo "opensearch-down   Stop the optional OpenSearch service"

	@echo "eval-baseline     Run all three Stage 2 smoke comparators"
	@echo "compare-runs      Compare the latest random and BM25 smoke Runs"
	@echo "agent-smoke       Run the deterministic smoke-only Agent Runtime"
	@echo "agent-eval        Run the fixed 12-task Stage 5 Agent Eval Harness"
	@echo "query-set-smoke   Build the source-bounded exploratory Query set"
	@echo "bad-cases-smoke   Run the fixed 59-Query behavioral diagnostics"

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

data-download:
	bash scripts/download_esci.sh

data-esci-validate:
	$(VENV_PYTHON) -m search_quality.data.cli --validate-only

data-esci-build:
	$(VENV_PYTHON) -m search_quality.data.cli

catalog-index:
	$(VENV_PYTHON) -m search_quality.catalog.cli

api:
	$(VENV_PYTHON) -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 --no-access-log

agent-api-volcengine-macos:
	open scripts/start-volcengine-agent-plan-macos.command

opensearch-up:
	docker compose up -d opensearch

smoke-opensearch:
	@OPENSEARCH_ALLOW_INDEX_RESET=true $(VENV_PYTHON) -m search_quality.smoke \
		--backend opensearch --query "$(QUERY)"

opensearch-down:
	docker compose down

eval-baseline:
	$(VENV_PYTHON) -m search_quality.evaluation.cli \
		--profile "$(EVAL_PROFILE)" --ranker "$(EVAL_RANKER)"

compare-runs:
	$(VENV_PYTHON) -m search_quality.evaluation.compare_cli \
		--profile "$(EVAL_PROFILE)"

agent-smoke:
	@test -n "$(BASELINE_RUN_ID)" -a -n "$(CANDIDATE_RUN_ID)" || \
		(echo "set BASELINE_RUN_ID and CANDIDATE_RUN_ID" >&2; exit 2)
	$(VENV_PYTHON) -m search_quality.agent.cli \
		--baseline-run-id "$(BASELINE_RUN_ID)" \
		--candidate-run-id "$(CANDIDATE_RUN_ID)"

agent-eval:
	$(VENV_PYTHON) -m search_quality.agent_eval.cli \
		--suite stage5-retrieval-v1

query-set-smoke:
	$(VENV_PYTHON) -m search_quality.query_constructor.cli

bad-cases-smoke:
	$(VENV_PYTHON) -m search_quality.bad_cases.cli

web:
	@echo "Stage 7 command reserved: the Web product is not implemented in Stage 0." >&2
	@exit 2

clean:
	rm -rf .pytest_cache build dist src/*.egg-info
