## A timeout kill can leave `test.log` with an unflushed tail

When `sim_timeout` expires, the simulator can be killed before it flushes output, so `test.log` can end mid-line and its last bytes do not locate the stop. Follow the [timeout triage order](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#triaging-sim-hit-timeout) before raising the limit.
