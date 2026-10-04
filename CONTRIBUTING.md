# Contributing

CimriHook measures what long agent sessions cost and changes only the agents' own settings. Bug
reports, measurements from other setups and small, focused pull requests are welcome.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest -q
```

CI runs the same four commands on Linux and macOS. The package needs Python 3.12 and has no runtime
dependencies.

## Conventions

- Pure functions that return values; classes only where the code talks to an external system.
- Strict typing (`mypy --strict`): no `Any`, no loose dicts; structured data is a frozen dataclass.
- Every parameter is explicit: no default values, no flag parameters that switch behaviour.
- Errors are specific and carry the context needed to debug them (request, status, response). No
  catch-all handlers and no silent fallbacks.
- Comments and docstrings are English and describe the current state, not the history.
- Tests prefer real files and real processes over mocks (see `tests/test_guard_e2e.py`), and cover
  stable data or pure transformations, not coverage numbers.

## Measurements

The numbers in the README come from `cimrihook bench-run` on one account, one model and one task
family. The most useful contribution is a measurement from another setup: another model, effort
level, plan or kind of work. Open a "Measurement" issue with:

- the output of `cimrihook limits` (aggregates only: no paths, no project names),
- the first lines of `cimrihook doctor`,
- your plan, the model and its effort level.

A new benchmark task is a JSON file in `bench/tasks/` (a repository pinned to a commit, a test
command and the one-line bugs to inject); see the existing files and `cimrihook bench-run --help`.
Report A/B results with `cimrihook bench-report`, which prints the cost ratio with its confidence
interval and whether run success was shown to be non-inferior.
