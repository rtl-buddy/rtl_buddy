## Run in parallel

The default `--dispatch local` runs tests sequentially in the current process. For parallel execution:

```bash
rb regression --dispatch local-parallel -j 4
rb regression --dispatch slurm
```

- Dispatch implies shared builds. RTL Buddy expands each suite, creates one build job for the suite's unique compile keys (two chained jobs when [verilation is split off](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#split-verilation-from-the-c-build)), runs the dependent simulation jobs and combines their results.
- `local-parallel` uses subprocesses on the current host and needs no scheduler. It cannot enforce `resources:` reservations or collect usage telemetry.
- Slurm needs a Linux submit host, Slurm client commands and a filesystem shared with the compute nodes.

See [Parallel Dispatch](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/) for cluster configuration, resources, failure recovery and job accounting.
