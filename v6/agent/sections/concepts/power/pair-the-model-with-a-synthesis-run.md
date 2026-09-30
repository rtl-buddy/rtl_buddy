## Pair the model with a synthesis run

A complete physical model needs the synthesis run's per-module cells and area and the power run's per-instance watts in one artefact directory. Each run publishes into its own directory by default. If the suites are split into `synth/` and `power/`, or the runs have different names, the halves land apart: `rb phys instance` exits 2 on the synthesis directory, and `rb phys module` finds no modules in the power run's.

`phys-run` pairs them explicitly:

```yaml
runs:
  - name: demo_power_static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
```

- The value names a run in the `synth.yaml` that `synth-path` points at. The model and manifest are published into that run's `artefacts/<run>/`; the log, reports and netlist copy stay in the power run's own directory. The synthesis directory need not exist yet.
- A `phys-run` that names no entry in `synth.yaml`, or resolves outside the project, is a configuration error.
- `phys-run` requires `netlist-source: synth`.
- One directory holds one power half. Two power runs with the same `phys-run`, such as a static and a dynamic analysis of one design, overwrite each other. Leave `phys-run` off runs whose breakdowns must stay apart.
- Removing or changing `phys-run` leaves the old directory's power half in place. Rerun the analysis or delete the directory.

`phys-run` does not force a merge. The halves merge only when the synthesis and the power run used the same netlist. Otherwise the run warns and publishes its half alone; rerun the synthesis and the power analysis over one netlist. A later synthesis likewise keeps the power rows only if its netlist is unchanged. A `pnr` source never merges. See [Read a model with only one half](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/#read-a-model-with-only-one-half).
