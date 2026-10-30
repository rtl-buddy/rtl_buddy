## Building Wheels And Sdists

```bash
uv build
uv build --wheel
uv build --sdist
```

Artifacts land in `dist/`. Inspect wheel and sdist contents when changing `[tool.hatch.build.targets.*]`; both must include the package and docs, not tests or development configuration.
