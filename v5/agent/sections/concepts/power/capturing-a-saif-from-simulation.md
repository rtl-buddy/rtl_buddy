## Capturing a SAIF from simulation

`rb saif` converts an FST or VCD waveform trace into SAIF v2.0 (backward direction):

```bash
# 1. Run the sim in debug mode so it produces an FST
rb -M debug test csr_smoke

# 2. Convert the FST to SAIF
rb saif verif/<suite>/artefacts/csr_smoke/dump.fst verif/<suite>/artefacts/csr_smoke/dump.saif

# 3. Reference it from power.yaml (activity.saif: ...) and run rb power
rb power demo_power_dynamic_saif -c power/demo/power.yaml -l 1000
```

The converter walks the trace hierarchy, computes per-bit T0/T1/TX/TZ time-in-state and TC toggle counters, and emits SAIF in the trace's native timescale so values stay exact integers. Memory-array elements (FST `[N]` vars) are skipped — they don't correspond to gate-level nets in the synth netlist.

When the SAIF is rooted at the testbench (e.g. `tb_top`), pass `scope: "tb_top/u_dut"` so OpenROAD knows where in the SAIF tree the design top lives.
