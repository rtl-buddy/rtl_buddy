## Name the expected failure

A negative control fails in two ways when the check it exists for regresses: the check stays silent, and something downstream, a watchdog or a data mismatch, fails instead. A bare marker grades both `XFAIL`. `xfail_match` names the failure the marker expects:

```yaml
- name: guard_neg
  xfail_strict: true
  xfail_match: "ECP-1017"
```

- The regex is searched, not anchored, with Python `re` syntax. Quote it in YAML.
- It is searched in the first error/fatal line the grader recorded, then in the result desc. A matching `FAIL` becomes `XFAIL`.
- The first error/fatal line is the sim log's first `ERR:`/`FAT:` line; under `uvm:`, the first `UVM_ERROR`/`UVM_FATAL` report; for a failure from fired SVA assertions, the first `Assertion failed` line; for a simulator that exited nonzero after `PASS`, the first assertion failure or Verilator `%Error`/`%Fatal` line. The result carries it as `first_error`. Other flows and graders use the desc alone.
- A `FAIL` that does not match stays `FAIL`, with desc `xfail: failed for a different reason (expected /ECP-1017/): <desc>`, and the `*_suite.xfail` event reports `excused: false` with that reason.
- A pass is graded as without `xfail_match`: `XPASS`, a failure only under `xfail_strict`.
- It is accepted on every flow that takes a marker. Without `xfail` or `xfail_strict`, or with an invalid regex, configuration loading fails.
