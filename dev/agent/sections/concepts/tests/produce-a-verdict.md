## Produce a verdict

For a non-UVM, non-cocotb test, print exactly one terminal marker to simulator stdout at the start of a line:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

Use `ERR:` or `FAT:` after `FAIL` to include the reason in the summary. If both terminal markers appear, `FAIL` wins and RTL Buddy logs a warning. If neither appears, the outcome is unknown: the result is `NA` and the run exits 1. A simulator exit code alone is not a non-UVM verdict, but a simulator that exits nonzero *and* prints no marker has aborted, and that combination is reported as `FAIL`.

For UVM, configure thresholds and let RTL Buddy parse the UVM Report Summary:

```yaml
uvm:
  max_warns: 0
  max_errors: 0
```

A missing or malformed UVM summary fails the test. cocotb tests use `cocotb_results.xml` instead; do not print transcript markers for them.

Setup hooks, filelist validation, compilation, and simulation timeout can also produce `FAIL` before transcript parsing.
