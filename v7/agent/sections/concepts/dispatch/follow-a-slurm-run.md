## Follow a Slurm run

For each suite, dispatch submits one build job that compiles each distinct build once, then groups simulations with identical resolved resources into Slurm arrays that start after the build succeeds. Where [verilation is split off](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/dispatch/#split-verilation-from-the-c-build), the build is two chained jobs. Results come back into the normal summary and exit status.

A compile or verilate failure fails its tests with the compiler's exit status and error lines. Unrelated builds continue; the build job exits 0 so dependent jobs can report results. Tests with identical inputs reuse the latest failed compile's status and transcript in `compile.log`.

License waits and out-of-memory failures are treated as transient, so the next test recompiles. Recognized memory signatures include `Killed signal terminated program`, `virtual memory exhausted`, `out of memory`, `Cannot allocate memory`, and make's `Error 137`. A transient failure cannot replace a recorded non-transient failure, including when a test with different inputs attempts compilation. A job that returns no result (scheduler kill, node failure, dependency failure) counts as failed.

`max-jobs-per-array` throttles each array, so total concurrency can approach the throttle times the number of arrays (or slices, for [large groups](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/dispatch/#split-large-groups-into-several-arrays)).

Verilator, VCS, and Icarus can share a build. Other builders, and builders with an absolute `builder-simv`, compile inside each simulation job, which then reserves the field-wise maximum of its simulation and compile reservations. If no planned test can share a build and none is fanned out over seeds, no build job is submitted (`dispatch.build_job_skipped`).
