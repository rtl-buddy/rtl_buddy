## FPGA timing is optional unless gated

A completed routed run reports PASS even with negative slack. Read `timing_met`, `wns_ns` and `failing_paths` for closure work, or set `require-timing-met: true` in `fpga.yaml` to fail the run on a miss. A `timing_met: null` cannot trigger the gate.
