## Check logs

`rtl_buddy` writes orchestration logs to `rtl_buddy.log` in the directory where it is run.

Simulation output for each test goes to `logs/{test_name}.log`. For convenience, the symlinks `test.log`, `test.err`, and `test.randseed` always point to the latest run.

For machine-readable output (useful with CI or AI agents):

```bash
uv run rb --machine test basic
```

In machine mode, `rtl_buddy.log` is written as JSON Lines and console output is plain text. See [For Agents](https://rtl-buddy.github.io/rtl_buddy/v2/agents/) for more on machine mode.
