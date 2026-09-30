## Check traceability

Run from the project tree:

```bash
rb spec list
rb spec check-design
rb spec check-coverage
```

- `list` finds blocks under `spec/`, or under `--spec-dir`.
- `check-design` reports whether each block has a linked model. `--design-dir` changes the search root.
- `check-coverage` reports the tests and formal verifications that declare each item. `--verif-dir` changes the simulation-suite search root.

Restrict either check to blocks with `--block`, repeated as needed:

```bash
rb spec check-design --block my_block
rb spec check-coverage --block ip_fifo --block ip_arbiter
```

- An unknown block is a configuration error.
- If a discovered `tests.yaml` cannot load, `check-coverage` reports the suite failure and exits nonzero rather than counting its items as uncovered. Machine output lists these under `suite_load_failures`.

Use `--machine` for structured output. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/) for all options and [YAML formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/) for schemas.
