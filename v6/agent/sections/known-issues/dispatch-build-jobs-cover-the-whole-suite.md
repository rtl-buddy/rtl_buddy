## Dispatch build jobs cover the whole suite

One suite build job compiles every unique compile key serially, so `compile.time` must cover their total and all simulation jobs wait for the complete build. Count `compile.start` events to estimate the work.

Under dispatch, `sweep` runs once on the head, while `preproc` runs in the build job and again in every simulation job. Make `preproc` idempotent. Write shared generated files atomically to `artifact_dir`; write run-dependent files to `run_artifact_dir`.

Simulation jobs rely on the build stamp to skip recompilation. A preprocessor that rewrites a filelist source changes its mtime, invalidates every stamp, and can trigger concurrent compiles into one directory. `compile.prebuilt_stamp_invalid` identifies this case. Avoid rewriting unchanged shared inputs.

A design compile error is reported as `CompileFail`, not `DispatchFail`. Infrastructure failures remain `DispatchFail`. A failed build may be retried inside a simulation reservation, so size that reservation to accommodate compilation for non-shareable or recovery paths.
