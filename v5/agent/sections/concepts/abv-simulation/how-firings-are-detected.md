## How firings are detected

`rb test` parses both `test.log` and `test.err` after simulation looking for lines matching:

```text
%Error: <file>:<line>: Assertion failed in <hier>: '<expr>'
```

Under Verilator's `--timing` flow the line is prefixed with the simulation time, e.g. `[500] %Error: tb_top.sv:32: Assertion failed in top.dut: 'assert' failed.`; the counter accepts the optional leading `[<time>] ` prefix. (Before this was handled, a fired assertion under `--timing` was missed and the test reported NA instead of FAIL.)

A non-zero count flips the result to FAIL regardless of the prior verdict — whether the log said PASS, said nothing (NA), or the sim aborted before any marker — and folds the prior result/description into the FAIL message so the truth still surfaces.
