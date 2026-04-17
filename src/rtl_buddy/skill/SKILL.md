---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, and regression.yaml.
---

# rtl_buddy

You are running [`rtl_buddy`](https://rtl-buddy.github.io/rtl_buddy/) — a Verilog/SV build and regression driver. This skill covers agent-specific conventions. Everything else lives in the docs site; fetch it when you need CLI or YAML detail.

A docs-query MCP tool is planned. Until it ships, use WebFetch on <https://rtl-buddy.github.io/rtl_buddy/> for authoritative reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is written as JSONL and console output is plain text:

```bash
rtl-buddy --machine <subcommand> ...
```

See <https://rtl-buddy.github.io/rtl_buddy/agents/> for the JSONL schema and exit codes (0 pass, 1 test failures, 2 fatal).

## Version check

Report the installed version at the top of every run summary:

```bash
rtl-buddy --version
```

This skill ships with the CLI, so its content matches the installed major. Surface any observed behavior differences in your summary.

## Multi-suite discovery and CWD rules

Tests are defined in one or more `tests.yaml` files, and their referenced testbench/model paths are typically relative. Running from the wrong directory causes path failures.

1. Discover suites:
   ```bash
   rg --files -g '**/tests.yaml'
   ```
2. Run `test` / `randtest` **from each suite's directory**.
3. Run `regression` from the repo root so `root_config.yaml` and `design/regression.yaml` resolve naturally.
4. Summarize results per suite, not just globally.

## Log locations

- `rtl_buddy.log` — JSONL in `--machine` mode; written to the CWD you invoked from.
- `logs/<test>.log`, `logs/<test>.err`, `logs/<test>.randseed` — per-test artifacts in the same CWD.
- `logs/<test>.compile.log` — written only on compile failure.
- Symlinks `test.log`, `test.err`, `test.randseed` point at the latest run.

For multi-suite runs, each suite directory has its own `rtl_buddy.log` and `logs/`. Report logs per suite.

## Where to look next

- CLI reference: <https://rtl-buddy.github.io/rtl_buddy/reference/cli/>
- YAML schemas and coverage workflow: <https://rtl-buddy.github.io/rtl_buddy/>
- Known issues: <https://rtl-buddy.github.io/rtl_buddy/known-issues/>
