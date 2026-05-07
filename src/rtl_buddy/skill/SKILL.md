---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, and regression.yaml.
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

- **`root_config.yaml`** — project root, platform/build defaults, regression default path.
- **`regression.yaml`** — repo-level suite list for `regression`.
- **`tests.yaml`** — suite-level tests/testbenches; run `test` and `randtest` from this directory.
- **`models.yaml`** — design source filelists referenced by `tests.yaml`.
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

- `rtl_buddy.log` is JSONL in `--machine` mode and is written to the invocation/suite directory.
- Single-run outputs live under `artefacts/<test>/`; `randtest` iterations use `artefacts/<test>/run-0001/` etc.
- Suite-root `test.log`, `test.err`, and `test.randseed` symlink to the latest run.
- Multi-suite runs have separate `rtl_buddy.log` and `artefacts/` per suite; summarize results per suite.

## Bugs & Improvements
If you discover a rtl_buddy bug or potential improvement, you can post an issue on GitHub <https://github.com/rtl-buddy/rtl_buddy/> documenting your findings, with permission from your user.
