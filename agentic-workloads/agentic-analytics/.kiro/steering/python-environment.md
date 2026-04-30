# Python Environment Management

## UV for Python environments

- Always use `uv` to manage Python virtual environments and dependencies in this project.
- The venv is located at `app/agentcore_strands/.venv` and was created with Python 3.10.
- Always use `uv run` prefix when executing Python commands to ensure the correct venv is used.
- Use `python3` (not `python`) as the interpreter name.

## Running tests

- Run tests from the `app/agentcore_strands` directory.
- Command pattern: `uv run python3 -m pytest <test_path> -v --tb=short -x`
- Example: `uv run python3 -m pytest lambda_tests/test_lambda_properties.py::TestClassName -v --tb=short -x`
- Do NOT use bare `python3 -m pytest` — always prefix with `uv run`.

## Installing dependencies

- Use `uv pip install <package>` from the `app/agentcore_strands` directory.
- Do NOT use `pip install` directly.

## Syntax checking

- Use `getDiagnostics` tool instead of `python3 -m py_compile` for syntax validation.
- Do NOT run `python3 -m py_compile` — it can hang.
