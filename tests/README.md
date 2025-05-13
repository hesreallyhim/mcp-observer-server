# MCP Monitor Server Tests

This directory contains tests for the MCP Monitor Server implementation.

## Test Structure

- `conftest.py` - Test fixtures and utilities
- `test_subscriptions.py` - Tests for subscription management
- `test_file_monitoring.py` - Tests for file change detection
- `test_resources.py` - Tests for resource access
- `test_mcpignore.py` - Tests for .mcpignore functionality
- `test_cli.py` - Tests for command-line interface

## Running Tests

You can run the tests using pytest:

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_subscriptions.py

# Run with increased verbosity
uv run pytest -v

# Run with full output capture
uv run pytest -s

# Run tests with specific markers (for example, only async tests)
uv run pytest -m asyncio
```

## Writing New Tests

When adding new tests, consider the following:

1. Create test fixtures in `conftest.py` if they will be used across multiple tests
2. Group tests by functionality to make it easier to understand what's being tested
3. Use descriptive test names that make it clear what they're testing
4. Make sure to test both success and failure cases
5. Clean up any resources created during tests

## Test Coverage Checks

You can run test coverage checks to ensure all functionality is properly tested:

```bash
# Install pytest-cov if not already installed
uv pip install pytest-cov

# Run tests with coverage report
uv run pytest --cov=mcp_monitor_server

# Generate HTML coverage report
uv run pytest --cov=mcp_monitor_server --cov-report=html
```

## Continuous Integration

These tests are automatically run in CI/CD pipelines to ensure changes don't break existing functionality.