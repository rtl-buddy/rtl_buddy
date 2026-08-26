## New state after migration

The target distribution mechanism is a normal Python project environment managed by `uv`, with `rtl_buddy` installed from PyPI:

```toml
# pyproject.toml
[project]
name = "your-rtl-project"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "rtl_buddy",
]
```

Then run `rtl_buddy` through that environment:

```bash
uv run rb --version
uv run rb test basic
```

This eliminates the submodule and replaces it with a package dependency recorded in `pyproject.toml` and locked in `uv.lock`.
