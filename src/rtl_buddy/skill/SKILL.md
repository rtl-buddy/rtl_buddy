---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, synthesis flows, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, regression.yaml, synth.yaml, or synth_regression.yaml.
---

# rtl_buddy

You are running rtl_buddy a Verilog/SV build and regression helper configured with YAML.

This skill covers agent-specific conventions. Use bundled docs first:
`rtl-buddy docs list`, `rtl-buddy docs show agents`, `rtl-buddy --machine docs show reference/yaml`.
Use <https://rtl-buddy.github.io/rtl_buddy/> only as a fallback reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL and console output is plain text.

See `rtl-buddy docs show agents` for the JSONL schema and exit codes (0 pass, 1 test failures, 2 fatal).

## Version check

Report `rtl-buddy --version` at the top of every run summary.
This skill ships with the CLI, so its content matches the installed major. Surface any observed behavior differences in your summary.

## YAML types

Use `rtl-buddy --machine docs show reference/yaml` for exact schemas.

- **`root_config.yaml`** — project root, platform/build defaults, regression default path, synthesis tool defaults (`cfg-synth-tools`).
- **`regression.yaml`** — repo-level suite list for `regression`.
- **`tests.yaml`** — suite-level tests/testbenches; run `test` and `randtest` from this directory.
- **`models.yaml`** — design source filelists referenced by `tests.yaml` and `synth.yaml`.
- **`synth.yaml`** — synthesis runs; `model` name is the top; `tool` selects `cfg-synth-tools` entry; `params`/`defines`/`tool_overrides` for per-run customization.
- **`synth_regression.yaml`** — repo-level synthesis suite list for `synth-regression`.
- **`specs.yaml`** — spec traceability data; consumed by `rtl-buddy spec`.

## Pass/fail detection

- UVM tests use configured report thresholds; cocotb testbenches use JUnit XML.
- Otherwise, `artefacts/<test>/test.log` must contain stdout starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line. Missing markers report `NA`; simulator exit code alone is not authoritative.
- See `rtl-buddy docs show agents` and `rtl-buddy docs show concepts/cocotb`.

## Multi-suite discovery and CWD rules

- Discover suites with `rg --files -g '**/tests.yaml'`.
- Run `test` / `randtest` from each suite directory.
- Run `regression` from the repo root.
- Summarize results per suite, not just globally.

## Artefact locations

- `rtl_buddy.log` — JSONL in `--machine` mode; written to the suite root (CWD you invoked from).
- `artefacts/<test>/test.log`, `test.err`, `test.randseed`, `coverage.dat` — sim outputs for a single run.
- `artefacts/<test>/compile.log`, `run.f` — compile outputs, always at the test root (not per run-id).
- `artefacts/<test>/run-0001/test.log` etc. — per-iteration outputs for `randtest`.
- `artefacts/<test>/dump.fst` — FST waveform produced by debug-mode builds (`-M debug`).
- Symlinks `test.log`, `test.err`, `test.randseed` at the suite root point at the latest run.
- For multi-suite runs, each suite directory has its own `rtl_buddy.log` and `artefacts/`; report logs per suite.
- Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`

## Waveform viewing

- `rb wave <test>` opens `artefacts/<test>/dump.fst` in Surfer (runs debug sim first if no FST exists).
- Signal layout files follow the convention `<test>.surfer` placed next to `tests.yaml` (e.g. `verif/sandbox/basic.surfer`). Use `variable_add <path>` and `zoom_fit` commands. See `verif/mem/tb_spsram.surfer` for a reference example.
- Surfer must be configured in `cfg-surfer` in `root_config.yaml`. Run `rtl-buddy docs show concepts/root-config` for the schema.

## Bugs & Improvements
If you discover a rtl_buddy bug or potential improvement, you can post an issue on GitHub <https://github.com/rtl-buddy/rtl_buddy/> documenting your findings, with permission from your user.
