.PHONY: install dev fmt lint type test test-pg check migrate serve worker docker up audit install-check upgrade-check release-check

install:
	uv sync

fmt:
	uv run ruff format .
	uv run ruff check . --fix

lint:
	uv run ruff format --check .
	uv run ruff check .

type:
	uv run mypy

test:
	uv run pytest

test-pg:
	OASIS_TEST_DATABASE_URL=$${OASIS_TEST_DATABASE_URL:-postgresql+asyncpg://oasis:oasis@localhost:5432/oasis_route_test} uv run pytest

check: lint type test

migrate:
	uv run oasis-route migrate up

serve:
	uv run oasis-route serve --migrate

dev:
	uv run oasis-route dev

worker:
	uv run oasis-route worker

docker:
	docker build -t oasis-route:local .

up:
	docker compose up -d --build

audit:  ## dependency vulnerabilities, licenses, secrets
	uv export --frozen --no-dev --all-extras --no-emit-project --format requirements-txt -o /tmp/oasis-req.txt
	uvx pip-audit -r /tmp/oasis-req.txt --disable-pip --progress-spinner off
	UV_PROJECT_ENVIRONMENT=/tmp/oasis-runtime uv sync --frozen --no-dev --all-extras
	/tmp/oasis-runtime/bin/python scripts/check_licenses.py
	gitleaks git --no-banner --redact --config .gitleaks.toml .

install-check:  ## build the wheel, pip-install it into a clean venv, migrate, bootstrap, serve
	scripts/install_check.sh

upgrade-check:  ## run the previous release with data, upgrade to this tree, verify nothing broke
	scripts/upgrade_check.sh

release-check: check audit install-check upgrade-check  ## everything in docs/RELEASE_CHECKLIST.md that can be automated
