# Justfile

# Run mutable development fixes (modifies code)
# uv run just m[utable]
alias m := mutable
mutable:
    uv sync -U
    uv run pyrefly infer --return-types --parameter-types --imports --containers
    uv format
    uv run ruff check --fix --unsafe-fixes
    uv run pytest -q

# Run immutable repository checks (read-only)
# uv run just i[mmutable]
alias i := immutable
immutable:
    uv sync --frozen
    uv run pyrefly check
    uv format --diff
    uv run ruff check
    uv audit
    uv run complexipy --suggest-refactors
    uv run pytest -q