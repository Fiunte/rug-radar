# M1 targets. Later milestones add the rest of HANDOFF section 2.
PY := .venv/bin/python
UNIT := rugwatch-collector.service
UNIT_DIR := $(HOME)/.config/systemd/user
LAPTOP ?= rugwatch-laptop
LAPTOP_REPO ?= rug-radar

.PHONY: setup test lint install-service status report-day1 report-week1 check-env backup

setup:            ## venv from the lock file; .env from the example if missing
	uv sync --python 3.12 --frozen
	@test -f .env || { cp .env.example .env && chmod 600 .env && echo "created .env from .env.example"; }

test:             ## unit tests + leakage tests on fixtures (from M2) + the no-signing grep
	$(PY) -m pytest

lint:
	.venv/bin/ruff check src tests
	.venv/bin/mypy

check-env:        ## which variables are set (names only) and whether each RPC tier answers
	$(PY) -m rugwatch.collector.main check-env

install-service:  ## install or update + (re)start the collector systemd user unit; survives logout and reboot
	mkdir -p $(UNIT_DIR)
	sed 's|@REPO@|$(CURDIR)|g' deploy/$(UNIT) > $(UNIT_DIR)/$(UNIT)
	systemctl --user daemon-reload
	systemctl --user enable $(UNIT)
	systemctl --user restart $(UNIT)
	loginctl enable-linger $(USER)
	@sleep 3; systemctl --user --no-pager status $(UNIT) | head -12

status:
	@$(PY) -m rugwatch.collector.report status

report-day1:
	@$(PY) -m rugwatch.collector.report day1

report-week1:
	@$(PY) -m rugwatch.collector.report week1

backup:           ## run on the DESKTOP: pull data/ from the laptop over SSH (LAPTOP = ssh host alias)
	@test ! -f $(UNIT_DIR)/$(UNIT) || { echo "this is the collector laptop: run make backup on the desktop"; exit 2; }
	ssh $(LAPTOP) 'cd $(LAPTOP_REPO) && .venv/bin/python -m rugwatch.collector.main snapshot-state'
	mkdir -p data/raw data/state
	rsync -a --partial --append-verify --info=stats1 $(LAPTOP):$(LAPTOP_REPO)/data/raw/ data/raw/
	rsync -a --info=stats1 $(LAPTOP):$(LAPTOP_REPO)/data/state/backup/collector.sqlite data/state/collector.sqlite
	@echo "backup done at $$(date -u +%FT%TZ); data/ holds $$(du -sh data | cut -f1)"
