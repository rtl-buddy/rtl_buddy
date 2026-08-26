## Opening FPV counterexamples (`rb wave-fpv`)

`rb wave-fpv <verif_name>` opens the SymbiYosys counterexample VCD for a failed [formal verification](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/) in Surfer:

```bash
uv run rb wave-fpv demo_fpv_counter_safety
```

It reads the same `fpv.yaml` (`-c`/`--fpv-config`, default `fpv.yaml`) to resolve the verification name, then opens the trace at `<dir of fpv.yaml>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` (first engine in sorted order). It opens the VCD in the `cfg-surfer` entry named `surfer-default` unless you pass `--surfer <name>`. Unlike `rb wave`, it just opens the VCD — there is no WCP annotation round-trip — so mainline Surfer suffices. It raises a clear error if the verification has not been run, the proof passed (no counterexample), or no engine produced a trace.
