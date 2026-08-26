## Capture SAIF activity

Convert a debug waveform with the built-in `rb saif` command:

```bash
rb -M debug test csr_smoke
rb saif verif/demo/artefacts/csr_smoke/dump.fst \
  verif/demo/artefacts/csr_smoke/dump.saif
rb power demo_power_saif -c power/demo/power.yaml -l 1000
```

The converter accepts FST or VCD and writes SAIF v2.0. If the trace starts at the testbench, configure `activity.scope` so OpenROAD can map the design top.
