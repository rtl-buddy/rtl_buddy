## Use a non-default or several Slurm clusters

When `sbatch-args` selects a cluster (`-M name` or `--clusters=name`), or `SBATCH_CLUSTERS` does, the array-size probe, polling, cancelling, and accounting all target that cluster. `sbatch-args` wins over the environment. A `max-wait` failure prints ready-to-run commands, such as `squeue -M alpha -j '77_[1-2]'`.

With several clusters selected (`--clusters=a,b` or `all`), Slurm picks the cluster at submit and no single array limit applies. Set `cfg-dispatch.max-array-size`, or `max-array-tasks` alone if that is the ceiling you know. The `dispatch.build_job_deduped` warning that names the job being waited on is not printed. The build job is still submitted with singleton, but a site with `disable_remote_singleton` may not serialise across clusters.
