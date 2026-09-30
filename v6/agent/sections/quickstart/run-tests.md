## Run tests

From a suite directory containing `tests.yaml`:

```bash
uv run rb test --list     # list tests
uv run rb test basic      # run one test
uv run rb test            # run every test
```

From another directory, name the suite:

```bash
uv run rb test basic --test-config path/to/tests.yaml
```

Outputs land beside `tests.yaml`, not in the directory you ran from. See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/).
