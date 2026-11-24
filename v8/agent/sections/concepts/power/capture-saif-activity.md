## Capture SAIF activity

Convert a debug waveform with `rb saif`, then run the analysis:

```bash
rb -M debug test csr_smoke
rb saif verif/demo/artefacts/csr_smoke/dump.fst \
  verif/demo/artefacts/csr_smoke/dump.saif
rb power demo_power_saif -c power/demo/power.yaml -l 1000
```

The converter accepts FST or VCD. If the trace starts at the testbench, set `activity.scope` to the design top.

Names are written in the form OpenROAD's `read_saif` reads, so one name cannot fail the whole read. An escaped Verilog name loses its leading backslash: the trace's `\u_a.n1` is written `u_a.n1`, and a bracket in a name is written `\[`. A net or instance whose name has no readable form is left out, an instance with everything beneath it, and `rb saif` warns with the count (`saif.names_skipped`). See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#rb-saif-leaves-out-names-read_saif-cannot-read).
