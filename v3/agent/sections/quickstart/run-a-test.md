## Run a test

Run the test named `basic` using `tests.yaml` in the current directory:

```bash
uv run rb test basic
```

Specify a different test config file:

```bash
uv run rb test basic --test-config path/to/tests.yaml
```

Run all tests in a config:

```bash
uv run rb test
```

List available tests without running them:

```bash
uv run rb test --list
```
