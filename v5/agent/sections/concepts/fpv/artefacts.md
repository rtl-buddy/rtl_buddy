## Artefacts

Per-run outputs land under the command root — `<dir of fpv.yaml>/artefacts/<run>/` (the artefact tree is anchored on the selected `fpv.yaml`'s directory, not your shell's cwd; see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/)):

| File | Contents |
|---|---|
| `fpv.log` | Full `sby` stdout/stderr |
| `fpv.f` | Generated filelist (stripped, deduplicated) |
| `fpv.sby` | Generated sby config (the file actually handed to `sby`) |
| `sby_workdir/status` | Sby's verdict file (`PASS`, `FAIL`, `UNKNOWN`, or `ERROR`) |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample VCD on failed properties |
| `sby_workdir/engine_<N>/logfile.txt` | Per-engine log |
| `vacuity_covers.sv` | Auto-generated sidecar module with one `cover property` per `\|->` antecedent (only when the vacuity pass ran) |
| `vacuity.sby` / `vacuity.log` / `vacuity_workdir/` | Secondary sby cover-mode pass for vacuity checks |
| `coi.ys` / `coi.log` | yosys script + log for the cone-of-influence pass (only when `coi: true`) |
