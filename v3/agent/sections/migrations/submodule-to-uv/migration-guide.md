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
