---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, and regression.yaml.
---

# rtl_buddy

You are running rtl_buddy, a Verilog/SV build and regression helper configured with YAML.

Start every run by reporting `rtl-buddy --version`.

Use local docs first when you need feature detail or schema reference:

- `rtl-buddy docs list`
- `rtl-buddy docs show agents`
- `rtl-buddy docs show quickstart`
- `rtl-buddy --machine docs show reference/yaml`

Use GitHub Pages at <https://rtl-buddy.github.io/rtl_buddy/> as a fallback reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL and console output is plain text.

See `rtl-buddy docs show agents` or <https://rtl-buddy.github.io/rtl_buddy/latest/agents/> for the JSONL schema and exit codes (0 pass, 1 test failures, 2 fatal).

## CWD rules

- Discover suites with `rg --files -g '**/tests.yaml'`.
- Run `test` and `randtest` from the suite directory.
- Run `regression` from the project root.

## Pass/fail rules

- If `tests.yaml` sets `uvm:`, rtl_buddy parses the UVM Report Summary.
- If a testbench has `cocotb:`, rtl_buddy parses cocotb JUnit XML. See `rtl-buddy docs show concepts/cocotb`.
- Otherwise, rtl_buddy parses `artefacts/<test>/test.log` and expects one stdout line starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line.
- Do not rely on simulator exit code alone for non-UVM pass/fail signaling.

## Artefact locations

- `rtl_buddy.log` lives in the directory where you invoked rtl_buddy.
- `artefacts/<test>/test.log`, `test.err`, `test.randseed`, and `coverage.dat` are per-test outputs.
- `artefacts/<test>/compile.log` and `run.f` are compile outputs at the test root.
- `artefacts/<test>/run-0001/` and friends hold per-iteration `randtest` outputs.
- `test.log`, `test.err`, and `test.randseed` in the suite root point to the latest run.
- In multi-suite flows, each suite keeps its own `rtl_buddy.log` and `artefacts/`.
