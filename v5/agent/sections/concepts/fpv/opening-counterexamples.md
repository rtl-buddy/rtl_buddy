## Opening counterexamples

```bash
# Open the CEX VCD for a failed verification in the configured surfer.
rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` reads the same `fpv.yaml` (`-c`/`--fpv-config`, default `fpv.yaml`) to resolve the verification name, then opens the trace at `<dir of fpv.yaml>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` (first engine in sorted order wins when more than one produced a trace). It opens the VCD in the `cfg-surfer` entry named `surfer-default` unless you pass `--surfer <name>`. Raises if the verification has not been run yet or the proof passed (no CEX was produced).
