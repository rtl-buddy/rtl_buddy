## Read timing and quality results

Use machine mode for automation:

```bash
rb --machine fpga demo_fpga > result.json
```

Vivado results can include LUT, FF, BRAM and DSP utilization; WNS, TNS and hold slack; timing status and failing paths; power; DRC counts; methodology warnings; and the bitstream path. openXC7 emits the smaller set described above.

To close timing:

1. Read `timing_met`, `wns_ns` and the worst `failing_paths`.
2. Compare `requirement_ns`, endpoints, logic depth and routing delay where available.
3. Change one constraint, RTL pipeline stage, placement choice or tool directive.
4. Rerun the same command and compare WNS.
5. Stop when timing closes or the change no longer helps.

Choose the change from the path evidence:

- An unrealistic requirement points to a wrong `create_clock`.
- A valid cross-domain or quasi-static path may need a false-path or multicycle exception. Confirm the functional relationship first; do not add exceptions just to silence a path.
- Logic-dominated delay suggests pipelining.
- Routing-dominated delay suggests congestion or placement work.

Methodology warnings are informational and do not change pass/fail.
