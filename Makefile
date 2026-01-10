.PHONY: help install install-dev test test-cov lint format typecheck clean docker-build docker-run

# Default target
help:
	@echo "Parser Maker - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      - Install production dependencies"
	@echo "  make install-dev  - Install development dependencies"
	@echo ""
	@echo "Testing:"
	@echo "  make test         - Run tests"
	@echo "  make test-cov     - Run tests with coverage"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint         - Run linter (ruff)"
	@echo "  make format       - Format code (ruff)"
	@echo "  make typecheck    - Run type checker (mypy)"
	@echo "  make check        - Run all checks (lint + typecheck + test)"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build - Build Docker image"
	@echo "  make docker-run   - Run in Docker container"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        - Remove build artifacts"

# Installation
install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"
	pre-commit install

# Testing
test:
	pytest tests/ -v

test-cov:
	pytest tests/ -v --cov=. --cov-report=term-missing --cov-report=html

test-fast:
	pytest tests/ -v -x --tb=short

# Code Quality
lint:
	ruff check .

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy bender core pipeline state cli integrations --ignore-missing-imports

check: lint typecheck test

# Docker
docker-build:
	docker build -t parser-maker:latest .

docker-run:
	docker run -it --rm \
		-v $(PWD):/app \
		-e GEMINI_API_KEY=$(GEMINI_API_KEY) \
		parser-maker:latest

# Cleanup
clean:
	rm -rf __pycache__ */__pycache__ */*/__pycache__
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov .coverage coverage.xml
	rm -rf *.egg-info build dist
	rm -rf logs/*.log

# Development helpers
run:
	python -m cli.main run $(PROJECT_PATH)

status:
	python -m cli.main status

resume:
	python -m cli.main resume
