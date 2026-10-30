## List the runs a project holds

A project accumulates one artefact directory per partition, corner and power mode. `rb phys runs` lists them, newest first (`--limit 0` for all):

```bash
rb phys runs
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

Each row shows the run name and top, the backends, the power mode and activity source, a configuration fingerprint, the `rb xplr` experiment when the run sits under `artefacts/xplr/<exp-id>/`, and the generation time. The row marked `*` is the newest, which the other verbs read by default.

The artefact directory of each run is printed under the table, ready to pass to `--phys-dir`. An unreadable manifest is listed with its error. A project with no physical artefacts lists nothing and exits 0.
