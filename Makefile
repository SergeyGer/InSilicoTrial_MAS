# ---------------------------------------------------------------------------
# Developer entry points. `make help` lists everything.
# ---------------------------------------------------------------------------
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
JRE_DIR := .toolchain/jre17
export PYTHONPATH := src

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel

.PHONY: venv
venv: $(VENV)/bin/activate ## Create the virtual environment

.PHONY: install
install: venv ## Install the package with dev, llm and tracking extras
	$(PIP) install -e '.[dev,llm,tracking]'

.PHONY: install-all
install-all: install ## Install everything, including Spark and gradient boosting
	$(PIP) install -e '.[spark,ml]'

.PHONY: jre
jre: ## Download a portable JRE into .toolchain (enables local Spark without root)
	@mkdir -p $(JRE_DIR)
	@if [ ! -x "$(JRE_DIR)/bin/java" ]; then \
		curl -sSL -o /tmp/insilico-jre.tar.gz "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jre/hotspot/normal/eclipse" && \
		tar -xzf /tmp/insilico-jre.tar.gz -C $(JRE_DIR) --strip-components=1; \
	fi
	@$(JRE_DIR)/bin/java -version

.PHONY: test
test: ## Run the fast test suite (excludes Spark)
	$(PY) -m pytest -q -m "not spark"

.PHONY: test-all
test-all: ## Run every test, including Spark engine tests
	JAVA_HOME=$(PWD)/$(JRE_DIR) PATH=$(PWD)/$(JRE_DIR)/bin:$$PATH $(PY) -m pytest -q

.PHONY: test-spark
test-spark: ## Run only the Spark engine tests
	JAVA_HOME=$(PWD)/$(JRE_DIR) PATH=$(PWD)/$(JRE_DIR)/bin:$$PATH $(PY) -m pytest -q -m spark

.PHONY: coverage
coverage: ## Run tests with coverage and write coverage.xml
	$(PY) -m pytest -q -m "not spark" --cov=insilico_trial_mas --cov-report=term-missing --cov-report=xml

.PHONY: lint
lint: ## Lint with ruff
	$(PY) -m ruff check src tests scripts

.PHONY: format
format: ## Auto-fix lint findings
	$(PY) -m ruff check --fix src tests scripts

.PHONY: types
types: ## Type-check with mypy
	$(PY) -m mypy

.PHONY: check
check: lint types test ## Lint, type-check and test

.PHONY: env-check
env-check: ## Print the environment checklist and the recommended engine
	$(PY) -m insilico_trial_mas.cli env-check

.PHONY: demo
demo: ## Run a small end-to-end simulation with the offline LLM
	$(PY) -m insilico_trial_mas.cli demo --patients 300 --epochs 6 --output-dir artifacts/demo

.PHONY: simulate
simulate: ## Run the local profile (override with PATIENTS=..., ENGINE=...)
	$(PY) -m insilico_trial_mas.cli simulate --config conf/simulation_local.yaml \
		--patients $(or $(PATIENTS),2000) --engine $(or $(ENGINE),local) --output-dir $(or $(OUT),artifacts/local)

.PHONY: simulate-spark
simulate-spark: ## Run the Spark engine locally on the driver (JAVA_HOME set automatically)
	JAVA_HOME=$(PWD)/$(JRE_DIR) PATH=$(PWD)/$(JRE_DIR)/bin:$$PATH \
		$(PY) -m insilico_trial_mas.cli simulate --config conf/simulation_local.yaml \
		--patients $(or $(PATIENTS),2000) --engine spark --output-dir $(or $(OUT),artifacts/spark)

.PHONY: train-model
train-model: ## Train and persist the physiology residual model
	$(PY) -m insilico_trial_mas.cli train-physiology --rows 20000

.PHONY: calibrate
calibrate: ## Check that simulated trials look clinically plausible
	$(PY) scripts/calibrate_protocol.py --patients 3000

.PHONY: verify-ui
verify-ui: ## Verify the UI (charts, payload, dashboard HTML, studio endpoints)
	$(PY) -m pytest -q tests/test_ui_dashboard.py tests/test_ui_studio.py

.PHONY: verify-repro
verify-repro: ## Prove that two runs with the same seed are identical
	$(PY) scripts/verify_reproducibility.py --patients 400 --epochs 4 --json

.PHONY: dashboard
dashboard: ## Build the interactive HTML dashboard for the newest run (RUN=<dir> to choose)
	@RUN_DIR="$${RUN:-$$(ls -dt artifacts/*/RUN-* 2>/dev/null | head -1)}"; \
	if [ -z "$$RUN_DIR" ]; then echo "no run found - execute 'make demo' first"; exit 1; fi; \
	$(PY) -m insilico_trial_mas.cli dashboard --run "$$RUN_DIR"

.PHONY: studio
studio: ## Serve the live Studio UI (STUDIO_PORT=8765 by default)
	$(PY) -m insilico_trial_mas.cli studio --config $(or $(CONFIG),conf/simulation_local.yaml) --port $(or $(STUDIO_PORT),8765)

.PHONY: notebook
notebook: ## Execute the verification notebook cells headlessly
	$(PY) scripts/run_notebook.py --patients 200 --epochs 3

.PHONY: architecture
architecture: ## Regenerate the architecture diagram and the social preview image
	$(PY) scripts/render_architecture.py
	$(PY) scripts/render_social_preview.py
	$(PY) scripts/render_ui_previews.py

.PHONY: github-setup
github-setup: ## Apply the GitHub About box and topics (needs GITHUB_TOKEN)
	$(PY) scripts/github_repo_setup.py

.PHONY: github-show
github-show: ## Print the live repository settings
	$(PY) scripts/github_repo_setup.py --show

.PHONY: publish
publish: ## Run the quality gates and publish the tree to GitHub
	bash scripts/publish_github.sh

.PHONY: docker-build
docker-build: ## Build the runtime image (target: runtime)
	docker build -t insilico-trial-mas:local .

.PHONY: docker-spark
docker-spark: ## Build the Spark image (JRE + PySpark + Delta)
	docker build --target spark -t insilico-trial-mas:spark .

.PHONY: docker-test
docker-test: ## Run pytest + ruff + mypy inside the image (fails the build on error)
	docker build --target test -t insilico-trial-mas:test .

.PHONY: docker-demo
docker-demo: ## One-shot demo run in a container, results in ./artifacts/docker
	mkdir -p artifacts/docker
	docker run --rm --user "$$(id -u):$$(id -g)" -v "$$PWD/artifacts/docker:/data/artifacts" \
		insilico-trial-mas:local demo --patients $(PATIENTS) --epochs $(EPOCHS) --output-dir /data/artifacts/demo

.PHONY: docker-studio
docker-studio: ## Studio UI in a container on http://127.0.0.1:8765 (Ctrl-C to stop)
	docker compose up studio

.PHONY: docker-shell
docker-shell: ## Interactive shell in the dev image with the source mounted
	docker compose --profile tools run --rm dev

.PHONY: docker-down
docker-down: ## Stop the compose stack and remove its volumes
	docker compose down -v

.PHONY: docker-clean
docker-clean: ## Remove the images built by this Makefile
	-docker image rm insilico-trial-mas:local insilico-trial-mas:spark insilico-trial-mas:test

.PHONY: iac-validate
iac-validate: ## terraform fmt + validate for the IaC module
	terraform -chdir=terraform fmt -recursive -check
	terraform -chdir=terraform init -backend=false -input=false
	terraform -chdir=terraform validate

.PHONY: bundle-validate
bundle-validate: ## Validate the Databricks Asset Bundle
	databricks bundle validate -t dev

.PHONY: clean
clean: ## Remove artefacts and caches
	rm -rf artifacts .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
