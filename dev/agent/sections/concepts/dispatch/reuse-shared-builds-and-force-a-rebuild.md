## Reuse shared builds and force a rebuild

A shared build is reused when no tracked input under the project root has changed. Inputs are compared by content, so a `preproc` hook that regenerates a file identically still reuses the build. See [Sharing compiled builds](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/tests/#sharing-compiled-builds-across-tests).

- `compile.build_reused` names the reused directory and its age; the test's `compile.log` adds the command a rebuild would run.
- `--rebuild` compiles even where the build would be reused, once per build directory per run. Under dispatch the build job performs the rebuild, or the simulation jobs do when the suite has no build job.
- `compile.build_lock_wait` means another process is compiling into the same directory. It is not a hang.
- Build jobs are named `rb-build-<hash>` from the suite directory, behind any [job tag](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#tag-job-names-for-one-caller), and use `--dependency=singleton`, so one build job per suite and tag runs at a time per cluster. A re-run after `Ctrl-C` waits for the interrupted build job, then reuses its build if the inputs are unchanged. Concurrent `--run-tag` runs of one suite serialise their builds while their simulations overlap.
- A `--dependency` in `sbatch-args` or `SBATCH_DEPENDENCY` is combined with the singleton (`afterok:7,singleton`). An expression using `?` cannot be combined, so the singleton is dropped. A `--job-name` in `sbatch-args` renames simulation jobs only, replacing any job tag.
- A site with `DependencyParameters=disable_remote_singleton` may not serialise across clusters. Pin one cluster with `-M` in `sbatch-args`, or see [known issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/).
- When a build fails, Slurm removes the dependent jobs. Override with `--kill-on-invalid-dep=no` in `sbatch-args`.
