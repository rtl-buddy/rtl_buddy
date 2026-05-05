---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, and regression.yaml.
---

# rtl_buddy

You are running rtl_buddy a Verilog/SV build and regression helper configured with YAML.

This skill covers agent-specific conventions. Use local docs if you need help:

- `rtl-buddy docs list`
- `rtl-buddy docs show agents`
- `rtl-buddy --machine docs show reference/yaml`

Use GitHub Pages at <https://rtl-buddy.github.io/rtl_buddy/> as a fallback reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL and console output is plain text.

See `rtl-buddy docs show agents` or <https://rtl-buddy.github.io/rtl_buddy/latest/agents/> for the JSONL schema and exit codes (0 pass, 1 test failures, 2 fatal).

## Version check

Report `rtl-buddy --version` at the top of every run summary.
This skill ships with the CLI, so its content matches the installed major. Surface any observed behavior differences in your summary.

## YAML types

rtl_buddy reads four YAML file types. See `rtl-buddy docs show reference/yaml` for exact schemas.

- **`root_config.yaml`** — project root. Selects platform, builders, builder modes, verible path, coverage config, and the default `regression.yaml` path. Discovered by walking up from the invocation directory.
- **`regression.yaml`** — lists the suite `tests.yaml` paths and reg-levels that `regression` iterates over.
- **`tests.yaml`** — per-suite. Declares `testbenches` (TB filelists) and `tests` that map test names to a model, model_path, and testbench. Lives in each verification suite dir.
- **`models.yaml`** — per-design. Maps model names to source/include filelists; consumed by `filelist` and referenced from `tests.yaml`.

## Test Pass/fail detection
- If `tests.yaml` sets `uvm:`, `rtl_buddy` parses the UVM Report Summary and applies the configured thresholds.
- If the testbench has a `cocotb:` block, `rtl_buddy` parses JUnit XML written by cocotb — no `PASS`/`FAIL` line needed. Run `rtl-buddy docs show concepts/cocotb` for setup.
- Otherwise, `rtl_buddy` parses `artefacts/<test>/test.log` and expects one stdout line starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line because the default failure parser expects it.
- Always use the `PASS` or `FAIL` markers as otherwise the result is ambiguous and shows `NA`.
- Do not rely on simulator exit code alone for non-UVM pass/fail signalling.

```systemverilog
if (test_passed) $display("PASS smoke completed");
else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

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
- Symlinks `test.log`, `test.err`, `test.randseed` at the suite root point at the latest run.
- For multi-suite runs, each suite directory has its own `rtl_buddy.log` and `artefacts/`; report logs per suite.
- Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`

## Bugs & Improvements
If you discover a rtl_buddy bug or potential improvement, you can post an issue on GitHub <https://github.com/rtl-buddy/rtl_buddy/> documenting your findings, with permission from your user.
