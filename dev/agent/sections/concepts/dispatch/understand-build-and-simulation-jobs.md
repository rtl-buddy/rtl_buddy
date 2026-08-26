## Understand build and simulation jobs

For each suite, dispatch:

1. Writes a plan and, when needed, submits one build job.
2. Builds each unique compile key serially. Compile keys fingerprint sources, flags, defines, and the resolved builder.
3. Groups simulations with identical resolved resources into Slurm arrays and gates them with `afterok` on the build.
4. Collects each worker's `result.json` into the normal summary and exit status.

Arrays group by resource tuple, not compile key. Tests may share a compiled executable while using different arrays, or share an array while using different builds. `max-jobs-per-array` is a `%N` throttle on each array; total concurrency can approach the throttle multiplied by the number of arrays.

Verilator, VCS, and Icarus can place outputs in a shared compile-key directory. Other builders, and builders with an absolute `builder-simv`, keep the build under the test artefact directory:

- Without shared-capable tests or seed fan-out, no separate build job is submitted; each simulation job compiles in its own directory.
- A fanned-out test still gets a build job to prevent concurrent compiles into the same test directory. Workers use the stamp left by that job.
- A job that may compile uses the field-wise maximum of its simulation and compile reservations. This protects against a missing or invalid prebuilt stamp.

When a build job exists, every dependent is submitted with `--kill-on-invalid-dep=yes`. A failed build therefore removes jobs that could never satisfy `afterok`; collection also cancels any `DependencyNeverSatisfied` remnants. A user-supplied `--kill-on-invalid-dep=no` in `sbatch-args` overrides the default.

A missing result from a scheduler kill, worker crash, or dependency failure is a failed row, not a dropped test. A compile failure for one compile key does not stop unrelated keys; the affected worker retries its own compile and reports the failure.
