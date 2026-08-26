## Run tests

From the suite directory:

```bash
rb test --list
rb test smoke
rb test smoke reset_error timeout
rb test --filter '^smoke_|_error$'
rb test
```

With no selection, `rb test` runs the suite. Explicit names run in command-line order and produce one combined results table. `--filter` uses a case-sensitive Python regex search against configured names; matches retain their `tests.yaml` order. Anchor the expression with `^` or `$` when position matters.

Explicit names and `--filter` are mutually exclusive. Duplicate or unknown names, an invalid regex, or a regex with no matches exits 2 before any test runs. Selection applies to configured base names before sweep expansion.

From another directory:

```bash
rb test smoke --test-config path/to/tests.yaml
```

Outputs remain beside `tests.yaml`; see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/).
