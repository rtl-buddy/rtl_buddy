# Migrating from Submodule to uv

Use this page to migrate an RTL project from the legacy `rtl_buddy` submodule flow to a pinned `uv` dependency.

## Current state

`rtl_buddy` is currently distributed as a git submodule under `tools/rtl_buddy` in your RTL project. Installation is via `pip install -r requirements.txt` where `requirements.txt` contains `-e tools/rtl_buddy`.

## Planned state

The target distribution mechanism is `uv` with a pinned git reference:

```toml
# pyproject.toml
[tool.uv.sources]
rtl_buddy = { git = "<repo-url>", tag = "v2.0.0" }
```

or equivalently:

```bash
uv add "rtl_buddy @ git+<repo-url>@v2.0.0"
```

This eliminates the submodule and replaces it with a locked dependency in `uv.lock`.

## Migration guide

This section will be updated when the `uv` distribution path is released. The migration will involve:

1. Removing the `tools/rtl_buddy` submodule from your project.
2. Adding a `pyproject.toml` or `uv` config pointing to the `rtl_buddy` git repo at a pinned tag.
3. Running `uv sync` to install.
4. Updating CI scripts that reference `tools/rtl_buddy` paths.

Watch the `rtl_buddy` release notes for the release that ships the `uv`-compatible package.
