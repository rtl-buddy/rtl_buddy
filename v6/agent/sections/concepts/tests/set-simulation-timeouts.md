## Set simulation timeouts

`sim_timeout` is a wall-clock limit in seconds and defaults to 60. For licensed simulators that may queue before running, add a builder-wide allowance to every test's timeout:

```yaml
cfg-rtl-builder:
  - name: vcs
    extra-sim-timeout: 900
```

- `--extra-sim-timeout N` overrides it for one command. 0 disables a configured allowance; negative values are rejected.
- It applies to simulation only, not compilation, and is forwarded to dispatch jobs.
- With VCS `-licqueue`, the timeout pauses while the license-queue banner is printing, for at most one hour.

### Triaging `Sim hit timeout`

`Sim hit timeout` means the wall-clock `sim_timeout` expired. It does not show the test is merely slow. Before raising the limit:

1. Compare sibling tests under the same builder. If they also stall, inspect the shared build, tool or environment.
2. Check whether `test.log` keeps advancing. Steady progress suggests a slow test; repeated activity suggests a functional wedge.
3. Find the last completed phase or transaction and inspect its RTL or testbench condition.
4. Confirm the resolved timeout, including builder and CLI allowances.

A killed simulator may not flush its output, so `test.log` can end mid-line and its last bytes are not the exact stop location.
