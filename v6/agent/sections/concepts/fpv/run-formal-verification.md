## Run formal verification

```bash
rb fpv
rb fpv demo_fpv_fifo -c fpv/demo_fifo/fpv.yaml
rb fpv -c fpv/demo_fifo/fpv.yaml --list
rb fpv-regression -c fpv_regression.yaml -l 1000
```

The summary shows the verdict, mode, depth, engines, engine result mix, runtime and counterexample path. sby gives no per-assertion verdicts; per-engine status is the finest detail.

A run passes when sby reports `PASS`. `FAIL`, `UNKNOWN`, `ERROR` or a nonzero exit fails it. In a regression, an entry above the selected level is `SKIP`.
