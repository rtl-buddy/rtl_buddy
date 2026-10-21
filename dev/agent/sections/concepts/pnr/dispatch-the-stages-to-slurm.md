## Dispatch the stages to Slurm

`--dispatch slurm` runs each stage as its own Slurm job, each held until the previous one succeeds, and implies `stages: true`. `rb pnr` stays on the submitting host, waits for the chain, then checks the result and streams out GDS there, so give that host memory for KLayout.

```yaml
runs:
  - name: alu_pnr
    # ...
    threads: auto
    resources:
      cpus: 4
      mem: 8G
      time: "02:00:00"
      stages:
        detailed_route: {cpus: 16, mem: 32G, time: "12:00:00"}
```

Each job's reservation comes from `resources.stages.<stage>`, then the run's `resources:`, then `cfg-dispatch.resources`, then built-in defaults (a run with no `resources:` warns `pnr.dispatch_resources_default`). With `threads: auto` each stage uses its job's CPUs. Jobs are named `rb:pnr:<run>:<stage>`; `cfg-dispatch.sbatch-args` apply to each. `--dispatch local` (default) runs stages in place.

- A job that fails, times out or runs out of memory fails the run at that stage (`fail_stage: dispatch` unless the stage itself failed) and Slurm removes the jobs after it.
- Ctrl-C, SIGTERM or SIGHUP cancels the run's jobs.
- If `rb pnr` itself is killed, its jobs keep running. Until they leave the queue, a new `rb pnr` of the same configuration refuses to start and prints the `scancel` command. On a host without `squeue`, that refusal lapses once the dispatch directory has been quiet for the sum of the stages' time limits (`pnr.stage_jobs_unchecked`), so check the queue before starting again. See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#an-unconfirmed-dispatched-pr-run-blocks-later-runs).
- `checkpoints/<run-id>/dispatch/` holds each job's id, Slurm log, rtl_buddy log and result.
