---
description: How to migrate an RTL project from the legacy rtl_buddy submodule flow to a uv-managed PyPI dependency.
---

# Migrating from Submodule to uv

Use this page to migrate an RTL project from the legacy `rtl_buddy` submodule flow to a `uv`-managed PyPI dependency.

## Existing state before migration

`rtl_buddy` is currently distributed as a git submodule under `tools/rtl_buddy` in your RTL project. Installation is via `pip install -r requirements.txt` where `requirements.txt` contains `-e tools/rtl_buddy`.

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

## Migration guide

Many legacy RTL repositories are not already Python projects. If your project does not have a `pyproject.toml`, create one first:

```bash
uv init --bare
```

Then add `rtl_buddy`:

```bash
uv add rtl_buddy
uv run rb --version
```

After the package install is working:

1. Remove the `tools/rtl_buddy` submodule from your project.
2. Remove `requirements.txt` if you have one, migrating the entries to `pyproject.toml` under dependencies.
3. Update local scripts and CI jobs from `tools/rtl_buddy/...` or `python -m rtl_buddy` inside the submodule checkout to `uv run rb ...`.
4. Commit `pyproject.toml` and `uv.lock` so other users and CI resolve the same environment.

If you need to hold a project on a specific release, pin the package version:

```bash
uv add "rtl_buddy==2.3.0"
```
