## Tests

```bash
uv run pytest                                # full suite
uv run pytest tests/test_cli_with_fixture.py # one file
uv run pytest -k "list"                      # by keyword
uv run pytest --cov --cov-report=term-missing
```

Coverage is opt-in; plain `pytest` stays fast.
