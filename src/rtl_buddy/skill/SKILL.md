---
name: rtl-buddy
description: Use rtl_buddy to run and debug SystemVerilog design, verification, implementation, and analysis workflows. Trigger this skill for rtl_buddy commands, results, artefacts, or YAML configuration.
---

# rtl_buddy

Run `rtl-buddy --version` at the start of every run and include it in the summary. For syntax and schemas, use `rb --help`, `rb <subcommand> --help`, `rtl-buddy docs list`, and `rtl-buddy docs show <page>`. Check `rtl-buddy docs show known-issues` when behavior is surprising.

## Always use `--machine`

Use `rb --machine ...` for every agent invocation. Structured result commands print a JSON envelope to stdout, and `rtl_buddy.log` becomes JSONL. Parse the envelope for results; exit 0 means pass, 1 means test failures, and 2 means fatal. `docs show` prints the requested page rather than an envelope. See `rtl-buddy docs show agents`.

## YAML map

Read exact fields with `rtl-buddy --machine docs show reference/yaml`.

- `root_config.yaml` selects shared platforms, builders, tools, and default regression paths.
- `tests.yaml` and `models.yaml` define simulation tests and design filelists.
- Flow files such as `synth.yaml`, `pnr.yaml`, `power.yaml`, `fpga.yaml`, `cdc.yaml`, and `fpv.yaml` define named runs.
- Regression YAML files collect suites or named runs; `mut.yaml` defines mutation campaigns and `specs.yaml` defines traceability items.

## Execution context

Outputs anchor on the config file, not the shell cwd. For example, `rb test -c path/to/tests.yaml` writes `artefacts/` and `rtl_buddy.log` under `dirname(tests.yaml)`; regression suites anchor on each suite's `tests.yaml`. Explicit CLI paths follow shell-relative semantics. See `rtl-buddy docs show concepts/execution-context`.

## Pass/fail detection

- UVM uses configured warning/error thresholds.
- cocotb uses `cocotb_results.xml`, not stdout markers.
- Other simulations should emit `PASS` or `FAIL` in `artefacts/<test>/test.log`; failures should also emit `ERR:` or `FAT:`.
- Formal runs use `artefacts/<run>/sby_workdir/status` as the authoritative verdict when present.

## Artefacts and debugging

Single simulations write `test.log`, `test.err`, `test.randseed`, and optional `coverage.dat` under `artefacts/<test>/`. Repeated runs use `run-0001/` subdirectories and keep latest-run symlinks at the suite root. Machine-readable coverage paths are returned in `payload.coverage.artefacts`.

For graph, coverage, waveform, formal, mutation, dispatch, FPGA, or design-space exploration workflows, open the matching concept page with `rtl-buddy docs show concepts/<page>` rather than relying on copied command recipes.
