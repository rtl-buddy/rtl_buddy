## Read timing and quality results

Use machine mode for automation:

```bash
rb --machine fpga demo_fpga > result.json
```

Vivado results may include LUT, FF, BRAM, and DSP utilization; WNS, TNS, and hold slack; timing status and failing paths; power; DRC counts; methodology warnings; and bitstream path. openXC7 emits its smaller metric set described above.

For timing closure:

1. Read `timing_met`, `wns_ns`, and the worst `failing_paths`.
2. Compare `requirement_ns`, endpoints, logic depth, and routing delay where available.
3. Change one relevant constraint, RTL pipeline, placement choice, or tool directive.
4. Rerun the same command and compare WNS.
5. Stop when timing closes or the chosen change no longer improves the result.

Use path evidence to select the change:

- An unrealistic requirement suggests correcting `create_clock`.
- A valid cross-domain or quasi-static path may need a false- or multicycle-path exception.
- Logic-dominated delay suggests pipelining.
- Routing-dominated delay suggests congestion or placement work.

Do not add timing exceptions merely to silence a path; confirm the functional relationship first. Post-route vectorless power is suitable for comparing runs, not signoff. Methodology warnings remain informational and do not change pass/fail.
