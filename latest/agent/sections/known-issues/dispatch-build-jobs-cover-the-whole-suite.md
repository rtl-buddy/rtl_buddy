## Dispatch build jobs cover the whole suite

One suite build job compiles every unique compile key, and all simulation jobs wait for the complete build. `cfg-dispatch.compile.parallel` compiles that many distinct builds at once inside that job, so `compile.time` must cover the longest batch rather than the serial total, and `compile.mem` must cover that many concurrent elaborations because only `cpus` is scaled for you. Count `compile.start` events to estimate the work.

The build job's reservation is `compile.cpus` multiplied by `min(parallel, planned tests)`. The cap is planned tests, not distinct compile keys: the head cannot know the keys without writing filelists on the submit host, which is the build job's own work. A suite whose tests share compile keys therefore reserves CPUs for build slots that never run — twenty tests over three keys with `parallel: 8` reserves eight builds' worth of CPUs for three. Set `parallel` to the expected distinct-build count, not to the test count, and confirm it against the `(build job)` row of the reservation advice.

Under dispatch, `sweep` runs once on the head, while `preproc` runs in the build job and again in every simulation job. Make `preproc` idempotent. Write shared generated files atomically to `artifact_dir`; write run-dependent files to `run_artifact_dir`.

`cfg-dispatch.compile.parallel` above 1 adds a second requirement: no config's `preproc` may mutate an input another config compiles. The build job runs every hook before any builder starts, because a config's compile key is only knowable after its own hook ran, so a hook that regenerates a suite-level file overwrites it for configs that have already been fingerprinted. At the default `parallel: 1` the job still runs `preproc` and compile per config in turn, so a generator hook that owns one shared file per config is safe there. Simulation jobs make the same demand at any setting, each re-running `preproc` on its own node.

Simulation jobs rely on the build stamp to skip recompilation. A preprocessor that rewrites a filelist source changes its mtime, invalidates every stamp, and can trigger concurrent compiles into one directory. `compile.prebuilt_stamp_invalid` identifies this case. Avoid rewriting unchanged shared inputs.

A design compile error is reported as `CompileFail`, not `DispatchFail`. Infrastructure failures remain `DispatchFail`. A failed build may be retried inside a simulation reservation, so size that reservation to accommodate compilation for non-shareable or recovery paths.
