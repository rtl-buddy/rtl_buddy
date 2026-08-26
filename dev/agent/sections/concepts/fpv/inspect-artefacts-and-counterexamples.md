## Inspect artefacts and counterexamples

Per-run output is anchored to the selected config at `<fpv.yaml dir>/artefacts/<run>/`:

| File | Contents |
|---|---|
| `fpv.log` | Full sby output |
| `fpv.f` | Generated stripped and deduplicated filelist |
| `fpv.sby` | Generated SymbiYosys configuration |
| `sby_workdir/status` | Overall verdict |
| `sby_workdir/engine_<N>/logfile.txt` | Engine log |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample when produced |
| `vacuity_covers.sv`, `vacuity.sby`, `vacuity.log`, `vacuity_workdir/` | Vacuity pass outputs |
| `coi.ys`, `coi.log` | COI and dead-assume analysis |

See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/) for artefact anchoring.

Open the first available counterexample in the configured Surfer instance:

```bash
rb wave-fpv demo_fpv_fifo
```

Use `-c` to select another `fpv.yaml` and `--surfer <name>` to override platform routing. The command errors when the verification has not run or produced no trace.

For SymbiYosys modes and engines, see the [SymbiYosys reference](https://symbiyosys.readthedocs.io/en/latest/reference.html).
