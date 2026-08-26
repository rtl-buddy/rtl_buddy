## Migration guide

This section will be updated when the `uv` distribution path is released. The migration will involve:

1. Removing the `tools/rtl_buddy` submodule from your project.
2. Adding a `pyproject.toml` or `uv` config pointing to the `rtl_buddy` git repo at a pinned tag.
3. Running `uv sync` to install.
4. Updating CI scripts that reference `tools/rtl_buddy` paths.

Watch the `rtl_buddy` release notes for the release that ships the `uv`-compatible package.
