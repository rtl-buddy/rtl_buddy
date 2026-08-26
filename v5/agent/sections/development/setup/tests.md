## Tests

The pytest suite under `tests/` is the primary correctness gate. CI runs it on every push and PR via `.github/workflows/test.yml`.

```bash
uv run pytest                                    # full suite
uv run pytest tests/test_cli_with_fixture.py     # one file
uv run pytest -k "list"                          # by keyword
uv run pytest --cov                              # with coverage summary
uv run pytest --cov --cov-report=term-missing    # show uncovered lines
uv run pytest --cov --cov-report=html            # write htmlcov/index.html
```

Coverage configuration lives in `[tool.coverage.*]` in `pyproject.toml` (source = `src/rtl_buddy`, excludes the bundled `skill/` and `docs/`). `pytest.ini` does not enable `--cov` by default so plain `pytest` stays fast; pass `--cov` explicitly when you want a coverage run.
