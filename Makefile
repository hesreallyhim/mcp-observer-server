.PHONY: test test-cov coverage-html lint fix format venv install install-dev run clean

# Default Python interpreter
PYTHON := python
# Python executable with activated virtual environment
VENV_PYTHON := .venv/bin/python
# Virtual environment path
VENV := .venv

# Install dependencies using uv
venv:
	uv venv

install: venv
	uv pip install -e .

install-dev: venv
	uv pip install -e ".[dev]"

# Run tests
test: install-dev
	$(VENV_PYTHON) -m pytest tests/

# Run tests with coverage
test-cov: install-dev
	$(VENV_PYTHON) -m pytest --cov=mcp_observer_server tests/ --cov-report=term

# Generate coverage HTML report
coverage-html: test-cov
	$(VENV_PYTHON) -m pytest --cov=mcp_observer_server tests/ --cov-report=html

# Run linting using ruff
lint: install-dev
	$(VENV_PYTHON) -m ruff check src/ tests/

# Auto-fix linting issues
fix: install-dev
	$(VENV_PYTHON) -m ruff check --fix src/ tests/

# Format code using ruff
format: install-dev
	$(VENV_PYTHON) -m ruff format src/ tests/

# Run the server
# run: install
# 	$(VENV_PYTHON) main.py

# Clean artifacts
clean:
	rm -rf .venv/
	rm -rf __pycache__/
	rm -rf src/mcp_observer_server/__pycache__/
	rm -rf tests/__pycache__/
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .coverage
	rm -rf htmlcov/

start:
	npx @modelcontextprotocol/inspector uv run src/mcp_observer_server/server.py
