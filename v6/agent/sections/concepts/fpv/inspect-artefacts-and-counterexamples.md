## Inspect artefacts and counterexamples

Output goes to `<fpv.yaml dir>/artefacts/<run>/` (see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/)):

| File | Contents |
|---|---|
| `fpv.log` | Full sby output |
| `fpv.sby` | Generated SymbiYosys configuration |
| `sby_workdir/status` | Overall verdict |
| `sby_workdir/engine_<N>/logfile.txt` | Engine log |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample, when produced |
| `vacuity.log`, `coi.log` | Vacuity and COI analyses |

Open the first counterexample in the configured Surfer:

```bash
rb wave-fpv demo_fpv_fifo
```

`-c` selects another `fpv.yaml` and `--surfer <name>` overrides platform routing. For sby modes and engines, see the [SymbiYosys reference](https://symbiyosys.readthedocs.io/en/latest/reference.html).
