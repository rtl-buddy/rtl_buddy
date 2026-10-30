## Run the coverage tail as a job

Under Slurm, `test`, `randtest`, and `regression` submit one coverage job after collecting all simulations. It merges raw coverage, builds `coverage-model.json`, exports LCOV, and writes the manifest. The head waits under `max-wait`, then reads its summary and machine payload.

No job is submitted without recorded coverage. A manifest-only tail (`--coverage-model none` without merge, LCOV, HTML, Coverview, directory or source summaries) runs in the head, as do tails without Slurm dispatch.

Size it with `cfg-dispatch.coverage`, which inherits `cfg-dispatch.resources` field by field and takes a `modes:` block. The tail only exists under a coverage build, so `modes.cov` is the usual place:

```yaml
cfg-dispatch:
  coverage:
    time: "01:00:00"
    modes:
      cov: {mem: 8G}
```

The raw merge runs up to `cpus` concurrent `verilator_coverage --write` processes, limited by `mem` at about eight times the largest raw database per process. The default is one. See [Merge and export results](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/coverage/#merge-and-export-results).

The job writes `cov_dir/` under the head's command root, logs to `.dispatch/coverage/` under the artefact root, and is named `rb:coverage`. If it fails, see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/coverage/#read-a-failed-merge).

Once accepted, the job is recorded in `.dispatch/coverage/run-<pid>-<token>.json`, like a fleet. If the head dies while waiting on it, the next run finds it there (see [Interrupted runs](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/dispatch/#interrupted-runs-warn-cancel-adopt)).
