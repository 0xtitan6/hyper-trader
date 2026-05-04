default:
    @just --list

setup:
    python3 -m venv .venv
    .venv/bin/pip install -e '.[dev]'

check: lint typecheck test

lint:
    .venv/bin/ruff check src tests
    .venv/bin/ruff format --check src tests

fmt:
    .venv/bin/ruff format src tests
    .venv/bin/ruff check --fix src tests

typecheck:
    .venv/bin/mypy src

test:
    .venv/bin/pytest

test-cov:
    .venv/bin/pytest --cov=src --cov-report=term-missing --cov-report=html

run:
    .venv/bin/python -m src.main
