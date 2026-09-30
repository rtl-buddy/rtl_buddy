## Coverage merging runs in the submitting process, with no timeout

The merge and LCOV exports run in the process that invoked `rb`, including under `--dispatch slurm`.

- A few hundred inputs can peak in the gigabytes. A memory cap on a shared submit host can kill the merge; coverage then reports `FAIL` and the command exits 1 (see [Read a failed merge](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#read-a-failed-merge)).
- There is no timeout, so a hung merge hangs the run.

Run the coverage command on a compute node, for example by submitting `rb regression --coverage-merge` as one job.
