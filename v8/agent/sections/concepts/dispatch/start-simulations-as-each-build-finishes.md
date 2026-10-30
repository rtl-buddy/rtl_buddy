## Start simulations as each build finishes

Simulation jobs wait on the suite's build job, but the build job releases each build's simulations as soon as that build is done, so a fast build does not wait for the slowest. A released array element still honours the array's throttle.

These jobs start only when the build job ends:

- Jobs whose build failed or left no usable build, and retried jobs.
- Every job of a suite whose `sbatch-args` has its own `--dependency=...`, because clearing it would drop your gate (`dispatch.gates_skipped`). An exported `SBATCH_DEPENDENCY` does not disable release (`dispatch.env_dependency_overridden`); put the expression in `sbatch-args` if it must hold these jobs.
- All jobs, when `scontrol` is not on the PATH of the compute node running the build job (`dispatch.release_unavailable`).
