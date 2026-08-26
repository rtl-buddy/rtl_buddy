## Pass/fail detection

cocotb writes a JUnit XML results file (`cocotb_results.xml`) instead of `PASS`/`FAIL` stdout lines. `rtl_buddy` parses this file automatically — do **not** add `$display("PASS …")` in cocotb tests. The `desc` field reports the first three failure messages with a `(+N more)` suffix when there are more.
