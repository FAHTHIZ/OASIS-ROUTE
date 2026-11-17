.PHONY: install dev fmt lint type test test-pg check migrate serve worker

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
