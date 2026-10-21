.PHONY: help venv install run docker-build docker-run docker-up docker-down docker-logs docker-push \
        seed analytics train benchmark eval-agent test test-unit test-integration lint format clean tree notebook

VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
-include .env
IMAGE_NAME ?= flight-graph-api
TAG     ?= latest

help:
	@echo "Flight Graph Explorer - available commands"
	@echo ""
	@echo "  Setup"
	@echo "    make venv              Create local virtualenv ($(VENV))"
	@echo "    make install           Install requirements into $(VENV)"
	@echo ""
	@echo "  Run"
	@echo "    make run               Run FastAPI locally on :8000 (Neo4j must be up)"
	@echo "    make docker-build      Build the API image ($(IMAGE_NAME):$(TAG))"
	@echo "    make docker-run        Start the full stack (foreground, Ctrl+C to stop)"
	@echo "    make docker-up         Start the full stack in the background"
	@echo "    make docker-down       Stop the stack"
	@echo "    make docker-logs       Follow logs of all services"
	@echo "    make docker-push       Push the API image to Docker Hub"
	@echo ""
	@echo "  Data & ML (against a running Neo4j)"
	@echo "    make seed              Load OpenFlights into Neo4j (idempotent)"
	@echo "    make analytics         Run GDS: PageRank, betweenness, Louvain"
	@echo "    make train             Train the route (link) prediction model"
	@echo "    make benchmark         PROFILE naive vs optimised queries -> docs/benchmarks.md"
	@echo "    make eval-agent        Evaluate the NL->Cypher agent on a question set"
	@echo ""
	@echo "  Quality"
	@echo "    make test              All tests + coverage (DB tests auto-skip if Neo4j is down)"
	@echo "    make test-unit         Unit / API / static Cypher tests only (no DB needed)"
	@echo "    make test-integration  Only the tests against a live Neo4j / running stack"
	@echo "    make lint              Run pylint (must stay >= 9.5)"
	@echo "    make format            Run black"
	@echo "    make clean             Remove caches and temp files"
	@echo "    make tree              Show project tree (depth 3)"
	@echo "    make notebook          Launch Jupyter for demo.ipynb"

venv:
	@if [ ! -d "$(VENV)" ]; then \
		python3 -m venv $(VENV); \
		$(PIP) install --upgrade pip; \
		echo "Created $(VENV)"; \
	else echo "$(VENV) already exists"; fi
	@echo "To activate: source $(VENV)/bin/activate"

install: venv
	$(PIP) install -r requirements.txt

run:
	NEO4J_URI=bolt://localhost:7687 $(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

docker-build:
	docker compose build

docker-run:
	@test -f .env || (echo "Missing .env -> cp .env.example .env" && exit 1)
	docker compose up --build

docker-up:
	@test -f .env || (echo "Missing .env -> cp .env.example .env" && exit 1)
	docker compose up --build -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-push: docker-build
	docker tag $(IMAGE_NAME):latest $(IMAGE_NAME):$(TAG)
	docker push $(IMAGE_NAME):$(TAG)

seed:
	NEO4J_URI=bolt://localhost:7687 $(PY) -m scripts.seed_data

analytics:
	NEO4J_URI=bolt://localhost:7687 $(PY) -m scripts.run_gds

train:
	NEO4J_URI=bolt://localhost:7687 $(PY) -m scripts.train_link_predictor

benchmark:
	NEO4J_URI=bolt://localhost:7687 $(PY) -m scripts.benchmark_queries

eval-agent:
	NEO4J_URI=bolt://localhost:7687 $(PY) -m scripts.eval_agent

test:
	NEO4J_URI=bolt://localhost:7687 $(VENV)/bin/pytest -q --cov=app --cov=scripts --cov-report=term-missing --cov-report=xml

test-unit:
	$(VENV)/bin/pytest -q -m "not integration" --cov=app --cov=scripts --cov-report=term-missing

test-integration:
	NEO4J_URI=bolt://localhost:7687 API_BASE_URL=http://localhost $(VENV)/bin/pytest -q -m integration

lint:
	$(VENV)/bin/pylint app scripts tests --fail-under=9.5

format:
	$(VENV)/bin/black app scripts tests -l 120

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} \; || true
	find . -type f -name "*.pyc" -delete || true
	rm -rf .pytest_cache .coverage coverage.xml htmlcov

tree:
	@if command -v tree >/dev/null 2>&1; then \
		tree -L 3 -I "node_modules|dist|.git|.venv|__pycache__"; \
	else find . -maxdepth 3 -not -path '*/\.*' -not -path './.venv*' | sort; fi

notebook:
	$(PIP) install -r requirements-notebook.txt
	$(VENV)/bin/jupyter notebook demo.ipynb
