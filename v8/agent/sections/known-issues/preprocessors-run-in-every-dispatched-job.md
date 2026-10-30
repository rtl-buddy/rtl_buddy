## Preprocessors run in every dispatched job

Under dispatch, `sweep` runs once on the head. `preproc` runs in the build job and again in every simulation job; where verilation is split off, the verilate job runs it for every config and the build job only for one config per build it still has to make. Make `preproc` idempotent, write shared generated files atomically to `artifact_dir`, and write run-dependent files to `run_artifact_dir`.

With `compile.parallel` above 1, `preproc` for later configs runs while earlier builds compile, so no config's `preproc` may modify an input that another config compiles. Configs with the same compile key share one build, and a hook that rewrites an input per config on one key makes every config after the first fail with `build_job.group_input_drift`. A hook should skip the write when a shared input would not change: a build whose inputs were rewritten while it compiled, even with identical bytes, is compiled again (`build_job.compile_inputs_moved`).
