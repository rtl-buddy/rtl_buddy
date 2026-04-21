---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, and regression.yaml.
---

# rtl_buddy

You are running [`rtl_buddy`](https://rtl-buddy.github.io/rtl_buddy/) — a Verilog/SV build and regression driver configured with YAML.

This skill covers agent-specific conventions. Use local docs first:

- `rtl-buddy docs list`
- `rtl-buddy docs show agents`
- `rtl-buddy --machine docs show reference/yaml`

GitHub Pages at <https://rtl-buddy.github.io/rtl_buddy/> remains the fallback reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL and console output is plain text.

See `rtl-buddy docs show agents` or <https://rtl-buddy.github.io/rtl_buddy/agents/> for the JSONL schema and exit codes (0 pass, 1 test failures, 2 fatal).

## Version check

Report `rtl-buddy --version` at the top of every run summary.
This skill ships with the CLI, so its content matches the installed major. Surface any observed behavior differences in your summary.

## YAML types

rtl_buddy reads four YAML file types. See `rtl-buddy docs show reference/yaml` for exact schemas.

- **`root_config.yaml`** — project root. Selects platform, builders, builder modes, verible path, coverage config, and the default `regression.yaml` path. Discovered by walking up from the invocation directory.
- **`regression.yaml`** — lists the suite `tests.yaml` paths and reg-levels that `regression` iterates over.
- **`tests.yaml`** — per-suite. Declares `testbenches` (TB filelists) and `tests` that map test names to a model, model_path, and testbench. Lives in each verification suite dir.
- **`models.yaml`** — per-design. Maps model names to source/include filelists; consumed by `filelist` and referenced from `tests.yaml`.

## Multi-suite discovery and CWD rules

- Discover suites with `rg --files -g '**/tests.yaml'`.
- Run `test` / `randtest` from each suite directory.
- Run `regression` from the repo root.
- Summarize results per suite, not just globally.

## Log locations

- `rtl_buddy.log` — JSONL in `--machine` mode; written to the CWD you invoked from.
- `logs/<test>.log`, `logs/<test>.err`, `logs/<test>.randseed` — per-test artifacts in the same CWD.
- `logs/<test>.compile.log` — written only on compile failure.
- Symlinks `test.log`, `test.err`, `test.randseed` point at the latest run.
- For multi-suite runs, each suite directory has its own `rtl_buddy.log` and `logs/`; report logs per suite.
- Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`
