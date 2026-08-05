---
name: rtl-buddy
description: Use rtl_buddy to run and debug SystemVerilog design, verification, implementation, and analysis workflows. Trigger for rtl_buddy commands, results, logs, or project YAML.
---

# rtl_buddy

Use this skill for agent workflow rules. Keep command and schema detail in the bundled docs.

## Start every run

- Run `rtl-buddy --version` and include the version in the summary.
- Use `--machine` for every agent invocation. Structured commands return one JSON envelope on stdout, `rtl_buddy.log` becomes JSONL, and exit codes are 0 for pass, 1 for test failures or no query match, and 2 for fatal errors.
- Discover syntax with `rb --help` and `rb <subcommand> --help`. Read bundled guidance with `rtl-buddy docs list`, `rtl-buddy docs show agents`, `rtl-buddy --machine docs show reference/yaml`, and `rtl-buddy docs show known-issues`.

## Find project inputs

- `root_config.yaml` holds shared tools and defaults.
- `tests.yaml` and `models.yaml` define simulation suites and design filelists.
- Flow configs include `synth.yaml`, `pnr.yaml`, `power.yaml`, `fpga.yaml`, `cdc.yaml`, `fpv.yaml`, and `mut.yaml`.
- Regression configs collect suites; `specs.yaml` defines traceability data. Use the YAML reference for exact fields.

## Work from the right context

- Outputs anchor to the config directory, not the shell cwd. `rb test -c path/tests.yaml` writes beneath `path/artefacts/`; each regression suite anchors to its own `tests.yaml`.
- Explicit CLI paths follow normal shell semantics. Run `test` from the suite directory when convenient and regression commands from the project root.
- For relational questions, prefer `rb --machine graph query "<question>"`, then cite source with the returned file/line or `rb hier-query`. Build the graph first when the query reports that none exists. Details: `rtl-buddy docs show concepts/graph`.

## Interpret results

- Parse the machine envelope and JSONL log; do not infer success from console prose.
- UVM pass/fail uses configured warning and error thresholds. cocotb uses `cocotb_results.xml`. Other simulations should emit `PASS` or `FAIL` in `artefacts/<test>/test.log`; formal status comes from `artefacts/<run>/sby_workdir/status` when present.
- Inspect `artefacts/<test>/test.log`, `test.err`, `test.randseed`, and `coverage.dat`; repeated randomized runs use `run-NNNN/`.
- When a result is surprising, read the relevant machine payload, log tail, and `rtl-buddy docs show known-issues` before changing configuration.

## Finish

Summarize the command, rtl_buddy version, exit code, structured verdict, failing items, seeds, and relevant artefact paths. Link to the appropriate bundled docs instead of copying schemas or feature manuals into this skill.
