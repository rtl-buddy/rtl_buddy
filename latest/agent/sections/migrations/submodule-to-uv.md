## Submodule to uv

Replace the legacy `tools/rtl_buddy` submodule and editable pip install with a `uv`-managed dependency:

```bash
uv init --bare        # only if there is no pyproject.toml yet
uv add rtl_buddy
uv run rb --version
```

1. Remove the `tools/rtl_buddy` submodule.
2. Fold any `requirements.txt` entries into `pyproject.toml` under `dependencies`, then delete `requirements.txt`.
3. Update local scripts and CI from `tools/rtl_buddy/…` / `python -m rtl_buddy` to `uv run rb …`.
4. Commit `pyproject.toml` and `uv.lock` so other users and CI resolve the same environment.

Use `uv add "rtl_buddy==<version>"` when the project needs an exact pin.
