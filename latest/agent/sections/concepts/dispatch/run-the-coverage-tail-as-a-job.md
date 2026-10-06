## Run the coverage tail as a job

Under `--dispatch slurm`, the coverage tail of `rb test` and `rb regression` runs as one job instead of on the submit host: the raw merge, the `coverage-model.json` build, the LCOV exports and the manifest. It is submitted once every simulation is collected, the head waits on it like the fleet (`max-wait` applies), then reads back the summary lines and machine payload. Nothing is submitted when no test recorded coverage. A manifest-only tail (`--coverage-model none` with no merge, LCOV, HTML, Coverview, directory or source summary requested) also runs in the head, since a job would only add a queue wait. Without dispatch and under `local-parallel` the tail runs in the head as before.

Size it with `cfg-dispatch.coverage`, which inherits `cfg-dispatch.resources` field by field and takes a `modes:` block. The tail only exists under a coverage build, so `modes.cov` is the usual place:

```yaml
cfg-dispatch:
  coverage:
    time: "01:00:00"
    modes:
      cov: {mem: 8G}
```

The job writes `cov_dir/` under the head's command root, logs to `.dispatch/coverage/` under the artefact root, and is named `rb:coverage`. If it fails, see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/#read-a-failed-merge).
