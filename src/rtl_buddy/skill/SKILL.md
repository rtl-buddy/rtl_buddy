---
name: rtl-buddy
description: Use rtl_buddy to orchestrate SystemVerilog compile/sim workflows, randomized tests, regressions, filelist generation, and verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, and regression.yaml.
---

# rtl_buddy

You are running `rtl_buddy`, a Verilog/SV build and regression helper configured with YAML.

This skill covers agent-specific conventions. Use local docs first:

- `rtl-buddy docs list`
- `rtl-buddy docs show agents`
- `rtl-buddy --machine docs show reference/yaml`

Use GitHub Pages at <https://rtl-buddy.github.io/rtl_buddy/> as fallback reference.

## Always use `--machine`

All agent invocations must use `--machine` so `rtl_buddy.log` is JSONL and console output is plain text.

## Version check

Report `rtl-buddy --version` at the top of every run summary. Surface any observed behavior differences.

## Pass/fail detection

- If `tests.yaml` sets `uvm:`, `rtl_buddy` parses the UVM Report Summary and applies the configured thresholds.
- Otherwise, `rtl_buddy` parses `logs/<test>.log` and expects one stdout line starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line because the default failure parser expects it.
- If no `PASS` or `FAIL` marker appears, the result is `NA`.
- Do not rely on simulator exit code alone for non-UVM pass/fail signalling.

```systemverilog
if (test_passed) $display("PASS smoke completed");
else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

## YAML files and CWD

- `root_config.yaml`, `regression.yaml`, `tests.yaml`, `models.yaml` drive discovery and test execution.
- Discover suites with `rg --files -g '**/tests.yaml'`.
- Run `test` / `randtest` from each suite directory.
- Run `regression` from the repo root.

## Log locations

- `rtl_buddy.log`: JSONL in `--machine` mode, written to the invocation directory.
- `logs/<test>.log`, `logs/<test>.err`, `logs/<test>.randseed`: per-test artifacts.
- `logs/<test>.compile.log`: written only on compile failure.
- `test.log`, `test.err`, `test.randseed`: symlinks to the latest run.

Next docs: `rtl-buddy docs show reference/cli`, `rtl-buddy docs show reference/yaml`, `rtl-buddy docs show known-issues`
