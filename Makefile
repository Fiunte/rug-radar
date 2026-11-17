# M1 targets. Later milestones add the rest of HANDOFF section 2.
PY := .venv/bin/python
UNIT := rugwatch-collector.service
UNIT_DIR := $(HOME)/.config/systemd/user

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

backup:           ## rsync data/ to the desktop: make backup DEST=user@desktop:/path/rug-radar-data/
	@test -n "$(DEST)" || { echo "usage: make backup DEST=user@host:/path/"; exit 2; }
	rsync -a --partial --info=stats1 data/ $(DEST)
