## Read a marker that was not applied

The summary table and `result.json` give the reason:

```text
<suite>  <test>  FAIL  xfail not applied (sim timeout): Sim hit timeout
```

- The result carries `fail_stage`: `setup`, `compile`, `sim_timeout`, `sim`, `dispatch` or `tool`.
- The `*_suite.xfail` event carries `excused: false` with the same reason, for machine consumers.
- A negative control that stopped compiling, or was killed before reaching the mismatch it exists to catch, therefore fails the run instead of reporting green.
