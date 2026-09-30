## Run a regression

```bash
uv run rb regression
```

The manifest is `./regression.yaml` when present, otherwise the path set in `root_config.yaml`. To choose one:

```bash
uv run rb regression --reg-config path/to/regression.yaml
```

See [Regressions](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/regressions/) for level filtering and parallel dispatch.
