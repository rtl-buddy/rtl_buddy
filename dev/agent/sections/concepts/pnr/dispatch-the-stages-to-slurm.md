## Dispatch the stages to Slurm

`--dispatch slurm` runs each stage as a Slurm job sized for that stage, held until the previous one succeeds, and implies `stages: true`. `--dispatch local` (default) runs stages in place. `rb pnr` waits on the submitting host, then checks the result and streams out GDS there, so give that host memory for KLayout.

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

- A job's reservation comes from `resources.stages.<stage>`, then the run's `resources:`, then `cfg-dispatch.resources`, then built-in defaults, with `pnr.dispatch_resources_default` warning when the run has no `resources:`.
- With `threads: auto` each stage uses its job's CPUs.
- Jobs are named `rb:pnr:<run>:<stage>`, and `cfg-dispatch.sbatch-args` apply to each.
- `checkpoints/<run-id>/dispatch/` holds each job's id, Slurm log, rtl_buddy log and result.
- A job that fails, times out or runs out of memory fails the run at that stage (`fail_stage: dispatch` unless the stage itself failed), and Slurm removes the later jobs.
- A job sbatch refuses because the queue is full (`MaxJobCount`) is resubmitted with backoff for up to 30 min (`dispatch.submit_deferred`; see [Dispatch](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#troubleshoot-reservations-arrays-and-waits)).
- Ctrl-C, SIGTERM or SIGHUP cancels the jobs.
- If `rb pnr` is killed, its jobs keep running, and a later `rb pnr` of the same configuration refuses to start until they leave the queue, printing the `scancel` command. Without `squeue`, the refusal lapses once the dispatch directory has been quiet for the sum of the stage time limits (`pnr.stage_jobs_unchecked`), so check the queue first ([Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#an-unconfirmed-dispatched-pr-run-blocks-later-runs)).
