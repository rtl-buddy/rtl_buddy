## Opening counterexamples

```bash
# Open the CEX VCD for a failed verification in the configured surfer.
rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` resolves the trace at `fpv/<suite>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` (first engine wins when more than one produced a trace). The configured surfer comes from the same `cfg-surfer` entry that `rb wave` uses; override with `--surfer <name>`. Raises if the verification has not been run yet or the proof passed (no CEX was produced).
