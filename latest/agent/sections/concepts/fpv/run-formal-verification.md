## Run formal verification

```bash
rb fpv
rb fpv demo_fpv_fifo -c fpv/demo_fifo/fpv.yaml
rb fpv -c fpv/demo_fifo/fpv.yaml --list
rb fpv-regression -c fpv_regression.yaml -l 1000
```

The summary reports the overall verdict, mode, depth, engines, engine result mix, runtime, and counterexample path. SymbiYosys does not provide structured per-assertion verdicts, so rtl_buddy reports per-engine status as the finest granularity.

A run is PASS when `sby_workdir/status` contains `PASS`, or when sby exits 0 without a status file. `FAIL`, `UNKNOWN`, `ERROR`, or a nonzero process exit is a failed run. A regression entry above the selected level is SKIP.

<a id="cone-of-influence-coverage"></a>
<a id="dead-assume-detection"></a>
<a id="vacuity-covers"></a>
