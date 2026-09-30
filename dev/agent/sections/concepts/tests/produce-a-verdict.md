## Produce a verdict

A plain (non-UVM, non-cocotb) test prints exactly one terminal marker at the start of a line on stdout:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

- `ERR:` or `FAT:` lines after `FAIL` put the reason in the summary.
- If both markers appear, `FAIL` wins and a warning is logged.
- If neither appears, the result is `NA` and the run exits 1. A nonzero simulator exit with no marker is an abort and reports `FAIL`.
- UVM tests are judged by thresholds on the UVM Report Summary; a missing or malformed summary fails the test.
- cocotb tests are judged from `cocotb_results.xml`. Do not print markers.

```yaml
uvm:
  max_warns: 0
  max_errors: 0
```

Setup hooks, filelist validation, compilation and timeouts can also produce `FAIL` before any transcript is parsed.
