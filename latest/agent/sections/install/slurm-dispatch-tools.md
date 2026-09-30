## Slurm dispatch tools

`--dispatch slurm` needs `sbatch`, `squeue`, and `scancel` on the submit host. Two more are recommended:

- `sacct` supplies the telemetry used for resource right-sizing.
- `scontrol` supplies `MaxArraySize` and `SchedulerParameters=max_array_tasks`. Without it, a group too large for one job array is not split. Set `cfg-dispatch.max-array-size`, and `cfg-dispatch.max-array-tasks` if the cluster caps tasks per array lower.

`scontrol` must also be on the `PATH` of the compute node that runs the build job. It lets each compile key's simulation jobs start as soon as that key is built; without it they wait for the whole build job.

Use `--dispatch local-parallel` for parallelism on one host with no Slurm dependency. See [Parallel dispatch](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#meet-the-slurm-requirements).
