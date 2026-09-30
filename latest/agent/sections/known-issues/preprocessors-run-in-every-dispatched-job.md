## Preprocessors run in every dispatched job

Under dispatch, `sweep` runs once on the head. `preproc` runs in each build job and again in every simulation job. Make `preproc` idempotent, write shared generated files atomically to `artifact_dir`, and write run-dependent files to `run_artifact_dir`.

With `compile.parallel` above 1, no config's `preproc` may modify an input that another config compiles. Configs with the same compile key share one build, and a hook that rewrites an input per config on one key makes every config after the first fail with `build_job.group_input_drift`.
