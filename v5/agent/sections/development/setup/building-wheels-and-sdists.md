## Building Wheels And Sdists

`rtl_buddy` uses `hatchling` plus `hatch-vcs`, so the version is derived from the latest git tag. Local builds work with `uv build`:

```bash
uv build              # both wheel and sdist
uv build --wheel
uv build --sdist
```

Artifacts land under `dist/`. The wheel ships `src/rtl_buddy/` plus the `docs/` tree (via `force-include`); the sdist ships the same source plus `README.md`, `LICENSE`, and `pyproject.toml`. Dev/CI files (`tests/`, `scripts/`, `.github/`, `mkdocs.yml`, `uv.lock`, agent guides, pre-commit config) are excluded — keep them that way when changing `[tool.hatch.build.targets.*]`.
