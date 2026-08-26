## Running a regression

Use the default config:

```bash
rtl-buddy regression
```

Specify a config file explicitly:

```bash
rtl-buddy regression --reg-config path/to/regressions.yaml
```

### Config resolution order

When `--reg-config` is not given, `rtl_buddy` resolves the regression config in this order:

1. `./regression.yaml` in the current working directory, if it exists
2. The path set in `root_config.yaml` under `cfg-rtl-reg.reg-cfg-path`

This means you can drop a `regression.yaml` at the repo root and run `rtl-buddy regression` without any flags, even if `root_config.yaml` points elsewhere.

### Regression levels

`rtl_buddy` filters tests by the `reglvl` value set in each `tests.yaml`. Use `--reg-level` and `--start-level` to select a range:

```bash
# Run all tests with reglvl <= 2000
rtl-buddy regression --reg-level 2000

# Run tests with reglvl in [1000, 3000]
rtl-buddy regression --start-level 1000 --reg-level 3000
```

The default is `--reg-level 0`, which runs only tests with `reglvl: 0` (must-run sanity tests).
