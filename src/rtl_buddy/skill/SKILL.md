---
name: rtl-buddy
description: Use rtl_buddy to run SystemVerilog tests, regressions, filelists, coverage, and Verible checks. Trigger this skill when asked to run or debug rtl_buddy commands or interpret root_config.yaml, tests.yaml, models.yaml, regression.yaml, or specs.yaml.
---

# rtl_buddy

You are running rtl_buddy, a Verilog/SystemVerilog build and regression helper configured with YAML.

Start every run by reporting `rtl-buddy --version`.

Use bundled docs before external references:
- `rtl-buddy docs list`
- `rtl-buddy docs show agents`
- `rtl-buddy --machine docs show reference/yaml`

Run all agent invocations with `--machine`. This makes `rtl_buddy.log` JSONL and keeps console output plain text.

Working-directory rules:
- Run `test` and `randtest` from the suite directory that contains `tests.yaml`.
- Run `regression` from the project root.
- Discover suites with `rg --files -g '**/tests.yaml'`.

Pass/fail rules:
- With `uvm:`, rtl_buddy parses the UVM Report Summary.
- With a `cocotb:` testbench, it parses `cocotb_results.xml`.
- Otherwise, `artefacts/<test>/test.log` must contain one stdout line starting with `PASS` or `FAIL`.
- On `FAIL`, also print an `ERR:` or `FAT:` line.
- If no PASS/FAIL marker appears, the result is `NA`; grep `rtl_buddy.log` for `postproc.no_markers`.

```systemverilog
if (test_passed) $display("PASS smoke completed");
else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

Artefacts:
- `rtl_buddy.log` lives in the invocation directory.
- `artefacts/<test>/` holds `compile.log`, `run.f`, `test.log`, `test.err`, `test.randseed`, and optional `coverage.dat`.
- `randtest` writes per-iteration logs under `artefacts/<test>/run-0001/` and later numbered dirs.
- `test.log`, `test.err`, and `test.randseed` at the suite root point to the latest run.

For YAML schema, plugins, spec traceability, and CLI details, cite the docs instead of restating them.
