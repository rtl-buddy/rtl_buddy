## Triaging `Sim hit timeout`

`Sim hit timeout` means the wall-clock limit expired; it does not identify a simulated-time watchdog or prove the test is merely slow. Before raising the limit:

1. Compare sibling tests under the same builder. If they also stall, inspect the shared build, tool, or environment.
2. Check whether timestamps or progress in `test.log` advance. Progress suggests a slow test; repeated activity suggests a functional wedge.
3. Identify the last completed phase or transaction and inspect its RTL or testbench condition.
4. Confirm the resolved timeout, including builder and CLI allowances.

A killed simulator may not flush its output, so `test.log` can end mid-line or at a power-of-two byte count. Do not treat its final bytes as the exact stop location.
