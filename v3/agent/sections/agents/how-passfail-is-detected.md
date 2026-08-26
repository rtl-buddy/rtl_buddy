## How pass/fail is detected

Agents authoring tests need to follow the parser that `rtl_buddy` actually uses:

- If `tests.yaml` sets `uvm:`, `rtl_buddy` parses the UVM Report Summary and compares it against `max_warns` / `max_errors`.
- Otherwise, `rtl_buddy` parses `artefacts/{test_name}/test.log` and expects one stdout line starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line because the default failure parser expects it.
- If neither `PASS` nor `FAIL` appears, the test result becomes `NA`.
- Do not rely on simulator exit code alone for non-UVM pass/fail signalling.

Minimal non-UVM example:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

In machine mode, the authoritative per-test outcome appears in the `postproc.completed` event's `result` and `desc` fields.
