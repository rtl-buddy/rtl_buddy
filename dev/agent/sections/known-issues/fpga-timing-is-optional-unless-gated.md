## FPGA timing is optional unless gated

A completed routed run reports PASS even with negative slack. Read `timing_met`, `wns_ns`, and `failing_paths` for closure work. Set `require-timing-met: true` in `fpga.yaml` to make a reported miss fail the run; an unsupported `timing_met: null` cannot trigger the gate.
