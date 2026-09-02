## Outputs and dispatch

Each run writes below the directory containing its `models.yaml`:

```text
artefacts/elab/<model>/<base-or-profile>/
  elab.f
  elab.log
  result.json
```

`elab.f` has unrolled includes and absolute path-valued entries. `result.json` records
the selected top, explicit and parsed source counts, error and warning counts,
elapsed time, peak worker memory, and pyslang version. A profile whose
`prepend_sources`, `append_sources`, or `include_dirs` entry is missing produces
a `FAIL` result at stage `filelist` instead of aborting the command, so a
regression continues with its remaining profiles. Machine mode returns the
same result payload and writes JSONL events to `rtl_buddy.log`.

`rb elab` dispatch is opt-in with `--dispatch`. `rb elab-regression` also honors
`cfg-dispatch.backend`. Profiles layer their `resources` over
`cfg-dispatch.resources`; `cpus` is both the scheduler request and pyslang
worker thread count. Slurm enforces memory and time reservations.
Local-parallel passes `cpus` to pyslang and uses its process-pool limit for
concurrency, but does not enforce memory or time.
