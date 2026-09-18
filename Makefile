PYTHON ?= python3.14
VENV_PYTHON := .venv/bin/python

.PHONY: help setup deps check configure check-r2
help:
	@echo "make tunnel     Open the configured Railway SSH tunnel; Ctrl+C closes it"
	@echo "make archive-plan  List other local archive candidates; no upload"
	@echo "make db-plan       Show configured database backup sources"
	@echo "make dev           Install development tools into .venv"
	@echo "make ci            Run local checks and offline tests"
	@echo "make r2-plan       Show the local R2 migration plan; no network"
	@echo "make r2-transfer   Copy the dataset to R2 and verify by read-back"
	@echo "make download-all  Download every missing image with live progress"
	@echo "make download   Download up to 20 missing images locally; source network calls"
	@echo "make reconcile  Match manifest IDs to local image filenames, offline"
	@echo "make inventory  Inventory local metadata; no cloud calls"
	@echo "make setup      Create Python 3.14 environment in .venv/"
	@echo "make deps       Install project dependencies into .venv/"
	@echo "make check      Check interpreter and syntax, offline"
	@echo "make check-r2   Check R2 bucket access, no object writes or reads"
	@echo "make configure  Save R2 connection settings locally"

setup:
	$(PYTHON) -c 'import sys; assert sys.version_info[:2] == (3, 14), "Python 3.14 is required"'
	$(PYTHON) -m venv .venv

check:
	$(VENV_PYTHON) -c 'import ast, pathlib, sys; assert sys.version_info[:2] == (3, 14); files = list(pathlib.Path("scripts").glob("*.py")); [ast.parse(p.read_text(), filename=str(p)) for p in files]; print("Python", sys.version.split()[0], "—", len(files), "scripts parsed; no cloud calls")'

configure:
	$(VENV_PYTHON) scripts/configure_r2.py

deps:
	@$(VENV_PYTHON) scripts/install_dependencies.py

check-r2:
	@$(VENV_PYTHON) scripts/check_r2.py

.PHONY: inventory
inventory:
	@$(VENV_PYTHON) scripts/inventory.py

.PHONY: reconcile
reconcile:
	@$(VENV_PYTHON) scripts/reconcile.py

LIMIT ?= 20
.PHONY: download
download:
	@$(VENV_PYTHON) scripts/download.py --limit $(LIMIT)

.PHONY: download-all
download-all:
	@$(VENV_PYTHON) scripts/download.py --all

.PHONY: r2-plan r2-transfer
r2-plan:
	@$(VENV_PYTHON) scripts/r2_transfer.py --plan

r2-transfer:
	@$(VENV_PYTHON) scripts/r2_transfer.py --all

.PHONY: test-r2
test-r2:
	@$(VENV_PYTHON) scripts/test_r2_transfer.py

.PHONY: dev lint test check-public ci
dev:
	@$(VENV_PYTHON) scripts/install_dependencies.py --dev

lint:
	@$(VENV_PYTHON) -m ruff check scripts
	@$(VENV_PYTHON) -m ruff format --check scripts

test:
	@$(VENV_PYTHON) scripts/run_tests.py

check-public:
	@$(VENV_PYTHON) scripts/check_public.py

ci: check check-public lint test
	@$(VENV_PYTHON) -m pip check

.PHONY: archive-plan db-plan backup-gc backup-vl-test
archive-plan:
	@$(VENV_PYTHON) scripts/archive_plan.py

db-plan:
	@$(VENV_PYTHON) scripts/db_backup.py --plan

backup-gc:
	@$(VENV_PYTHON) scripts/db_backup.py --name gc-local

backup-vl-test:
	@$(VENV_PYTHON) scripts/db_backup.py --name vl-test

.PHONY: restore-db
restore-db:
	@test -n "$(BACKUP)" || (echo "Set BACKUP to a snapshot directory"; exit 1)
	@$(VENV_PYTHON) scripts/db_restore.py --backup "$(BACKUP)"

.PHONY: tunnel tunnel-check
tunnel:
	@$(VENV_PYTHON) scripts/tunnel.py

tunnel-check:
	@$(VENV_PYTHON) scripts/tunnel.py --check
