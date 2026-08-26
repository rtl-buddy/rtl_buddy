## Run in parallel

The default `--dispatch local` runs tests sequentially in the current process. For parallel execution:

```bash
rb regression --dispatch local-parallel -j 4
rb regression --dispatch slurm
```

Dispatch implies shared builds. RTL Buddy expands each suite, creates one build job per unique compile key, then runs dependent simulation jobs and combines their normal results.

`local-parallel` uses subprocesses on the current host and needs no scheduler. It cannot enforce `resources:` reservations or collect usage telemetry.

Slurm dispatch requires a Linux submit host, Slurm client commands, and a filesystem shared with compute nodes. See [Parallel Dispatch](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/) for cluster configuration, resources, failure recovery, and job accounting.
