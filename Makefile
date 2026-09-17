PYTHON ?= python3.14
VENV_PYTHON := .venv/bin/python

.PHONY: help setup deps check configure check-r2
help:
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
