## Run tests

From a suite directory containing `tests.yaml`:

```bash
uv run rb test --list
uv run rb test basic
uv run rb test
```

The first command lists tests, the second runs `basic`, and the third runs every test. From another directory, identify the suite explicitly:

```bash
uv run rb test basic --test-config path/to/tests.yaml
```

Outputs land beside `tests.yaml`, not in the directory where you invoked the command. See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/).
