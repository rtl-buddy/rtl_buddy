## Submodule to uv

Replace the `tools/rtl_buddy` submodule and editable pip install with a `uv` dependency:

```bash
uv init --bare        # only if there is no pyproject.toml yet
uv add rtl_buddy
uv run rb --version
```

1. Remove the `tools/rtl_buddy` submodule.
2. Move any `requirements.txt` entries into `dependencies` in `pyproject.toml`, then delete `requirements.txt`.
3. Change local scripts and CI from `tools/rtl_buddy/…` or `python -m rtl_buddy` to `uv run rb …`.
4. Commit `pyproject.toml` and `uv.lock` so other users and CI resolve the same environment.

For an exact pin, use `uv add "rtl_buddy==<version>"`.
