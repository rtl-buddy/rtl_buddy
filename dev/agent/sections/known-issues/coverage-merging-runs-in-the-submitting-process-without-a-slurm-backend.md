## Coverage merging runs in the submitting process without a Slurm backend

Under `--dispatch slurm` the coverage tail (merge, model build, LCOV exports and manifest) runs as one job sized by `cfg-dispatch.coverage` (see [Run the coverage tail as a job](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#run-the-coverage-tail-as-a-job)). Without dispatch, and under `--dispatch local-parallel`, it runs in the process that invoked `rb`.

- A few hundred inputs can peak in the gigabytes. A memory cap on that host can kill the merge; coverage then reports `FAIL` and the command exits 1 (see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#read-a-failed-merge)).
- The merge has no time limit unless `cfg-coverage` sets `merge-timeout`, so a hung merge hangs the run.

Without Slurm, run the coverage command on a host with the memory it needs.
