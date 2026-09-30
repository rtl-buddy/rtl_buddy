## Run tests

From the suite directory:

```bash
rb test --list
rb test smoke
rb test smoke reset_error timeout
rb test --filter '^smoke_|_error$'
rb test
```

- With no selection, `rb test` runs the whole suite.
- Named tests run in command-line order and produce one table.
- `--filter` is a case-sensitive Python regex search over test names. Matches keep their `tests.yaml` order.
- Names and `--filter` are mutually exclusive. Duplicate or unknown names, an invalid regex, or a regex matching nothing exits 2 before any test runs.
- Selection applies to configured names, before sweep expansion.

From another directory, pass the suite explicitly. Outputs still land beside `tests.yaml` (see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/)):

```bash
rb test smoke --test-config path/to/tests.yaml
```
