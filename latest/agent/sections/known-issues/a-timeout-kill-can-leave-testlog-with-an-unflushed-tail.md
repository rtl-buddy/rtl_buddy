## A timeout kill can leave `test.log` with an unflushed tail

When `sim_timeout` expires, the simulator may be terminated before flushing output. `test.log` can end mid-line or at a power-of-two byte count, so its final bytes are not an exact stop location. Follow the [timeout triage order](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#triaging-sim-hit-timeout) before raising the limit.
