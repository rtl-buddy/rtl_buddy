## Artefacts

Per-run outputs land under `fpv/<run>/artefacts/`:

| File | Contents |
|---|---|
| `fpv.log` | Full `sby` stdout/stderr |
| `fpv.f` | Generated filelist (stripped, deduplicated) |
| `fpv.sby` | Generated sby config (the file actually handed to `sby`) |
| `sby_workdir/status` | Sby's verdict file (`PASS`, `FAIL`, `UNKNOWN`, or `ERROR`) |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample VCD on failed properties |
| `sby_workdir/engine_<N>/logfile.txt` | Per-engine log |
