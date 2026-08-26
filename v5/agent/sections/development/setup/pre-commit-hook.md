## Pre-Commit Hook

Install the `pre-commit` hook once so Ruff runs automatically on every commit:

```bash
uv tool install pre-commit
pre-commit install
```

To refresh the pinned hook version:

```bash
pre-commit autoupdate
```

CI enforces both `ruff check` and `ruff format --check` via `.github/workflows/lint.yml`, so it pays to catch issues at commit time.
