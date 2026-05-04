venv_bin := if os_family() == "windows" { ".venv/Scripts" } else { ".venv/bin" }

default:
    @just --list

setup:
    python3 -m venv .venv
    {{venv_bin}}/pip install -e '.[dev]'

check: lint typecheck test

lint:
    {{venv_bin}}/ruff check src tests
    {{venv_bin}}/ruff format --check src tests

fmt:
    {{venv_bin}}/ruff format src tests
    {{venv_bin}}/ruff check --fix src tests

typecheck:
    {{venv_bin}}/mypy src

test:
    {{venv_bin}}/pytest

test-cov:
    {{venv_bin}}/pytest --cov=src --cov-report=term-missing --cov-report=html

run:
    {{venv_bin}}/python -m src.main

preflight:
    {{venv_bin}}/python -m src.main --preflight
