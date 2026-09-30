## Capture SAIF activity

Convert a debug waveform with `rb saif`, then run the analysis:

```bash
rb -M debug test csr_smoke
rb saif verif/demo/artefacts/csr_smoke/dump.fst \
  verif/demo/artefacts/csr_smoke/dump.saif
rb power demo_power_saif -c power/demo/power.yaml -l 1000
```

The converter accepts FST or VCD. If the trace starts at the testbench, set `activity.scope` to the design top.
