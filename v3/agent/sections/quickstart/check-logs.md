## Check logs

`rtl_buddy` writes orchestration logs to `rtl_buddy.log` in the directory where it is run.

Simulation output for each test goes to `artefacts/{test_name}/`. A single run writes `test.log`, `test.err`, `test.randseed`, and (if coverage is enabled) `coverage.dat` directly there. Repeated runs (via `randtest`) write each iteration into a numbered subdirectory: `artefacts/{test_name}/run-0001/`, `run-0002/`, and so on. For convenience, the symlinks `test.log`, `test.err`, and `test.randseed` in the suite root always point to the latest run.

For machine-readable output (useful with CI or AI agents):

```bash
uv run rb --machine test basic
```

In machine mode, `rtl_buddy.log` is written as JSON Lines and console output is plain text. See [For Agents](https://rtl-buddy.github.io/rtl_buddy/v3/agents/) for more on machine mode.
