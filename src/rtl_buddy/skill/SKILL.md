---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, synthesis, place-and-route, CDC lint, formal property verification, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, regression.yaml, synth.yaml, synth_regression.yaml, pnr.yaml, cdc.yaml, or fpv.yaml.
---

# rtl_buddy

You are running rtl_buddy a Verilog/SV build and regression helper configured with YAML.

This skill covers agent-specific conventions. For CLI usage, `rb --help` and `rb <subcommand> --help` are the first stop, then bundled docs:
`rtl-buddy docs list`, `rtl-buddy docs show agents`, `rtl-buddy --machine docs show reference/yaml`.
Use <https://rtl-buddy.github.io/rtl_buddy/> only as a fallback reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL. Structured result commands, including `rb docs list` but excluding `rb docs show`, print a single JSON envelope to **stdout** on exit — parse this for results.

See `rtl-buddy docs show agents` for the stdout envelope schema, JSONL log format, and exit codes (0 pass, 1 test failures, 2 fatal).

## Version check

Report `rtl-buddy --version` at the top of every run summary.
This skill ships with the CLI, so its content matches the installed major. Surface any observed behavior differences in your summary.

## Config files

Use `rtl-buddy --machine docs show reference/yaml` for exact schemas.

- `root_config.yaml` sets project defaults and tool/platform entries.
- `tests.yaml` and `regression.yaml` drive sim suites; run `test`/`randtest` from the suite directory and `regression` from the repo root.
- `models.yaml` lists design sources used by sim, synth, P&R, CDC, FPV, and hierarchy commands.
- `synth.yaml`, `pnr.yaml`, `power.yaml`, `cdc.yaml`, and `fpv.yaml` configure implementation/analysis runs.
- `specs.yaml` holds spec traceability data consumed by `rtl-buddy spec`.

## Pass/fail detection

- UVM tests use configured report thresholds; cocotb testbenches use JUnit XML.
- Otherwise, `artefacts/<test>/test.log` must contain stdout starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line. Missing markers report `NA`; simulator exit code alone is not authoritative.
- See `rtl-buddy docs show agents` and `rtl-buddy docs show concepts/cocotb`.

## Multi-suite runs

- Discover suites with `rg --files -g '**/tests.yaml'`.
- Summarize results per suite, not just globally.

## Artefact locations

- `rtl_buddy.log` — JSONL in `--machine` mode; written to the suite root (CWD you invoked from).
- `artefacts/<test>/test.log`, `test.err`, `test.randseed`, `coverage.dat` — sim outputs for a single run.
- `artefacts/<test>/run-0001/test.log` etc. — per-iteration outputs for `randtest`.
- `artefacts/<test>/dump.fst` — FST waveform produced by debug-mode builds (`-M debug`).
- For multi-suite runs, each suite directory has its own `rtl_buddy.log` and `artefacts/`; report logs per suite.
- Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`

## Waveform viewing

- `rb wave <test>` opens `artefacts/<test>/dump.fst` in Surfer (runs debug sim first if no FST exists).
- Surfer must be configured in `cfg-surfer` in `root_config.yaml`. Run `rtl-buddy docs show concepts/root-config` for the schema.
