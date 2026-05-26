---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, synthesis, place-and-route, CDC lint, formal property verification, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, regression.yaml, synth.yaml, synth_regression.yaml, pnr.yaml, cdc.yaml, or fpv.yaml.
---

# rtl_buddy

Run `rtl-buddy --version` at the top of every run summary.

Use this skill for agent-specific workflow rules only. For command syntax or schema details, start with `rb --help`, `rb <subcommand> --help`, `rtl-buddy docs list`, `rtl-buddy docs show agents`, and `rtl-buddy --machine docs show reference/yaml`.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL. Structured result commands, including `rb docs list` but excluding `rb docs show`, print a single JSON envelope to **stdout** on exit — parse this for results.

See `rtl-buddy docs show agents` for the stdout envelope schema, JSONL log format, and exit codes (0 pass, 1 test failures, 2 fatal).

## YAML map

Use `rtl-buddy --machine docs show reference/yaml` for exact fields.

- `root_config.yaml` configures platforms, builders, coverage, waveform, synth, P&R, CDC, FPV, and default regression paths.
- `tests.yaml` is suite-local and defines testbenches plus tests. Invoke from anywhere with `-c <path>`; outputs anchor on the config dir (see Execution context below).
- `models.yaml` defines design filelists referenced by tests, synth, CDC, and FPV.
- `regression.yaml`, `synth_regression.yaml`, `cdc_regression.yaml`, and `fpv_regression.yaml` are repo-level suite lists.
- `synth.yaml`, `pnr.yaml`, `power.yaml`, `cdc.yaml`, and `fpv.yaml` define named runs for those flows.
- `specs.yaml` feeds `rb spec` traceability commands.

## Pass/fail detection

- UVM uses configured warning/error thresholds.
- cocotb uses `cocotb_results.xml`, not `PASS` or `FAIL` stdout markers.
- Other sims should emit `PASS` or `FAIL` in `artefacts/<test>/test.log`; add an `ERR:` or `FAT:` line when reporting failure.
- Formal runs use `artefacts/<run>/sby_workdir/status` as the authoritative verdict when present.

## Execution context

Outputs anchor on the **config file**, not your shell's cwd. `rb test -c path/to/tests.yaml` puts `artefacts/<test>/...` and `rtl_buddy.log` under `dirname(tests.yaml)`; same rule for `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, `models.yaml`. For `regression`, each suite anchors on its own `tests.yaml`; orchestration log under `regression.yaml`. Explicit CLI input/output paths (`-o out.svg`, `rb filelist <model> out.f`) follow shell semantics — relative to your cwd. Discover multi-suite layouts with `rg --files -g '**/tests.yaml'`; summarize per suite. Reference: `rtl-buddy docs show concepts/execution-context`.

## Artefact locations

- `artefacts/<test>/test.log`, `test.err`, `test.randseed`, `coverage.dat` — sim outputs for one run.
- `artefacts/<test>/run-0001/test.log` etc. — per-iteration outputs for `randtest`.
- `artefacts/<test>/dump.fst` — FST waveform from debug-mode builds (`-M debug`).
- Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`

## Waveform viewing

- `rb wave <test>` opens `artefacts/<test>/dump.fst` in Surfer (runs debug sim first if no FST exists).
- Surfer must be configured in `cfg-surfer` in `root_config.yaml`. Run `rtl-buddy docs show concepts/root-config` for the schema.
