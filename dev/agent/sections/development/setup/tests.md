## Tests

```bash
uv run pytest                                # full suite
uv run pytest tests/test_cli_with_fixture.py # one file
uv run pytest -k "list"                      # by keyword
uv run pytest --collect-only -q              # collection and case count
uv run pytest --durations=25                 # runtime without coverage overhead
uv run pytest --cov --cov-report=term-missing
```

Coverage is opt-in; plain `pytest` stays fast. Coverage includes Python subprocesses and enforces a combined statement/branch floor of 87%. CI also checks an 80% floor for each elaboration and Tcl worker, so missing subprocess measurements fail visibly. For a focused coverage investigation, select the affected source with `--cov=rtl_buddy.<module>` and set an appropriate `--cov-fail-under` for that subset; the full-suite floor applies to CI.

CI uses managed Python 3.12 and the graph extractor extra:

```bash
uv sync --managed-python --python 3.12 --group dev --extra graph-extract
uv run --managed-python --python 3.12 pytest --durations=25
```

Compare timings using the same interpreter, optional dependencies and tool availability. Read setup as well as call durations. Optional EDA tests probe their tools when selected and skip when prerequisites are absent; inspect skips with `-rs`. Keep a full run when changing shared fixtures or test infrastructure. Test authoring and deletion rules are in [Engineering Guidelines](https://rtl-buddy.github.io/rtl_buddy/dev/development/guidelines/#test-design).
