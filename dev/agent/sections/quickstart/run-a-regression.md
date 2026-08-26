## Run a regression

```bash
uv run rb regression
```

This uses `./regression.yaml` when present, then the path configured in `root_config.yaml`. To choose another manifest:

```bash
uv run rb regression --reg-config path/to/regression.yaml
```

See [Regressions](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/regressions/) for level filtering and parallel dispatch.
