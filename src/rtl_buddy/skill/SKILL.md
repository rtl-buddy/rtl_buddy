---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, synthesis, place-and-route, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, regression.yaml, synth.yaml, synth_regression.yaml, or pnr.yaml.
---

# rtl_buddy

Run `rtl-buddy --version` first and report it in every summary.

Use `--machine` for agent-driven runs so `rtl_buddy.log` is JSONL and console output is plain text.
CLI help lives at `rb --help` / `rb <subcommand> --help`; bundled docs live at `rtl-buddy docs list`, `rtl-buddy docs show agents`, and `rtl-buddy --machine docs show reference/yaml`.

## Working directory rules

- Discover suites with `rg --files -g '**/tests.yaml'`.
- Run `test` and `randtest` from the suite directory, or pass an explicit `--test-config`.
- Run `regression`, `synth-regression`, and `spec` from the repo root.
- Summarize multi-suite runs per suite, not only globally.

## YAML quick map

- `root_config.yaml` - platform defaults, builders, tool config, and default regression path.
- `regression.yaml` - repo-level suite list for `regression`.
- `tests.yaml` - suite-local tests and testbenches.
- `models.yaml` - design filelists referenced by tests and synth runs.
- `synth.yaml` - synthesis entries and per-run overrides.
- `synth_regression.yaml` - repo-level list for `synth-regression`.
- `pnr.yaml` - P&R entries that consume prior synth artefacts.
- `specs.yaml` - spec traceability data for `rb spec`.

Use `rtl-buddy --machine docs show reference/yaml` for exact schemas.

## Pass and fail detection

- UVM tests use configured report thresholds; cocotb testbenches use JUnit XML.
- Otherwise `artefacts/<test>/test.log` must contain a stdout line starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line.
- Missing result markers report `NA`; simulator exit code alone is not authoritative.
- See `rtl-buddy docs show agents` and `rtl-buddy docs show concepts/tests` for examples.

## Artefacts and debugging

- `rtl_buddy.log` is written in the current working directory.
- `artefacts/<test>/test.log`, `test.err`, `test.randseed`, `coverage.dat`, `compile.log`, and `run.f` are the primary sim outputs.
- `artefacts/<test>/run-0001/` and friends hold per-iteration outputs for `randtest`.
- `artefacts/<test>/dump.fst` is the debug-mode waveform.
- Suite-root symlinks `test.log`, `test.err`, and `test.randseed` point to the latest run.

## Waveforms and next docs

- `rb wave <test>` opens `artefacts/<test>/dump.fst` and uses an optional `<test>.surfer` file next to `tests.yaml`.
- If no FST exists yet, `rb wave` runs a debug sim first.
- For deeper feature docs, use `rtl-buddy docs show concepts/root-config`, `rtl-buddy docs show concepts/wave`, `rtl-buddy docs show reference/cli`, and `rtl-buddy docs show known-issues`.
