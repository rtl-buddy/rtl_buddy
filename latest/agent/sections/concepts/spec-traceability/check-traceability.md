## Check traceability

Run from the project tree:

```bash
rb spec list
rb spec check-design
rb spec check-coverage
```

- `list` discovers blocks under `spec/` or `--spec-dir`.
- `check-design` reports whether each block has a linked model. Use `--design-dir` to change the search root.
- `check-coverage` reports the tests and formal verifications that declare each item. Use `--verif-dir` to change the simulation-suite search root.

Filter either check to one or more blocks:

```bash
rb spec check-design --block my_block
rb spec check-coverage --block ip_fifo --block ip_arbiter
```

An unknown block is a configuration error. If a discovered `tests.yaml` cannot load, `check-coverage` reports the suite failure and exits nonzero instead of treating its items as uncovered. Machine output includes `suite_load_failures`.

Use the global `--machine` flag for structured output. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/) for all options and [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/) for schemas.
