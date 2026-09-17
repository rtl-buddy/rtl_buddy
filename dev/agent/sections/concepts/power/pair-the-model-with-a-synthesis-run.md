## Pair the model with a synthesis run

A complete physical model needs both halves — the synthesis' per-module cells and area, the power run's per-instance watts — and the two halves meet in one artefact directory. Each run publishes into its own by default, so a merged model is what a power run named after the synthesis it reads *and* configured in the same directory produces. Split the suites into `synth/` and `power/`, or rename either run, and each half lands in a directory of its own: `rb phys instance` exits 2 on the synthesis' directory and `rb phys module` reports no modules in the power run's, with nothing saying why.

`phys-run` says the pairing out loud:

```yaml
runs:
  - name: demo_power_static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
```

The value names a run in the `synth.yaml` that `synth-path` already points at, and the power half is published into that run's `artefacts/<run>/` — the directory the synthesis writes its own half into. It is a run name, not a path: the directory is derived, so moving the synthesis suite moves the pairing with it. Everything else this run writes — the log, the reports, its copy of the netlist — stays in the power run's own artefact directory, and the manifest names them there.

The named directory need not exist yet. A power run may land first and the synthesis fill the other half later, which is what publishing into a directory chosen rather than inherited is for. A `phys-run` that names no entry in the referenced `synth.yaml`, or that resolves outside the project because `synth-path` reaches into another checkout, fails the run as any other configuration error does. `phys-run` requires `netlist-source: synth`: a `pnr`-source run records no netlist hash, so its half can never merge with a synthesis' and pointing it at one would replace the module rows rather than complete them.

One directory holds one power half. Two power runs that name the same `phys-run` — a static and a dynamic analysis of one design, say — publish into it in turn, and the model there describes whichever ran last; a failed rerun of either withdraws the rows that are there. Leave `phys-run` off the runs whose breakdowns are to be kept apart, and they publish into their own directories as before.

The manifest's header names the run whose publication it is, so a shared directory reads as the synthesis or as the power run depending on which published last, and that is the name `rb phys runs` lists. Each half's own block names its producer either way. Changing or removing `phys-run` leaves the previous directory's power half where it is, as renaming any run leaves its artefact directory behind: re-run the analysis, or delete the directory.

**Naming the pairing does not make it.** The netlist sha256 both producers record is still the whole of the merge gate, and it decides exactly as before. What changes is that a refusal is no longer silent: a run given a `phys-run` whose module rows were counted off a netlist it did not read logs a warning and publishes its half alone. Re-run the synthesis and the power analysis over one netlist to pair them. See [Read a model with only one half](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#read-a-model-with-only-one-half).
