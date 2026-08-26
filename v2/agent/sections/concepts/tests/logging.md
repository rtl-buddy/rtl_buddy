## Logging

`rtl_buddy` writes orchestration output to `rtl_buddy.log` in the directory where it is invoked.

Per-test simulation output goes to:

- `logs/{test_name}.log` — full simulation output
- `logs/{test_name}.err` — stderr
- `logs/{test_name}.randseed` — the seed used

The symlinks `test.log`, `test.err`, and `test.randseed` in the current directory always point to the most recent test run.

For machine-readable logs (JSON Lines), use `--machine`. See [For Agents](https://rtl-buddy.github.io/rtl_buddy/v2/agents/).
