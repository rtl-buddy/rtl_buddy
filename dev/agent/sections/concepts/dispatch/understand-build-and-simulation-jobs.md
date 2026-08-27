## Understand build and simulation jobs

For each suite, dispatch:

1. Writes a plan and, when needed, submits one build job.
2. Builds each unique compile key. Compile keys fingerprint sources, flags, defines, and the resolved builder.
3. Groups simulations with identical resolved resources into Slurm arrays and gates them with `afterok` on the build.
4. Collects each worker's `result.json` into the normal summary and exit status.

The build job compiles one key at a time by default. `cfg-dispatch.compile.parallel` raises that to N distinct builds compiled concurrently inside the same job. Concurrency is over distinct compile keys, never over tests: configs sharing a key compile one after another, the later ones short-circuiting on the first one's build stamp, because two builders writing one build directory is what `compile.prebuilt_stamp_invalid` reports. Configs whose resolved `builder-simv` output is one file — an absolute pin, or a relative spelling whose `..` escapes the per-test workspace — are grouped the same way even though their compile directories differ, because the executable they write is one path. A config whose compile fails is still reported per test, and the job still exits 0 so its `afterok` dependents run.

Preprocessing hooks always run serially, and their position relative to compilation depends on `parallel`. At the default `parallel: 1` the job runs `preproc` and then the compile for one config before touching the next, so a hook that regenerates a shared input cannot overwrite what an earlier config is about to compile. Above 1 the compile key is only knowable after that config's `preproc` has run, so every hook runs first and the builders then overlap: raising `parallel` requires that no config's `preproc` mutate another config's inputs. Simulation jobs already require this — each `rb _test-job` re-runs its own `preproc` concurrently on its own node.

Arrays group by resource tuple, not compile key. Tests may share a compiled executable while using different arrays, or share an array while using different builds. `max-jobs-per-array` is a `%N` throttle on each array; total concurrency can approach the throttle multiplied by the number of arrays.

Verilator, VCS, and Icarus can place outputs in a shared compile-key directory. Other builders, and builders with an absolute `builder-simv`, keep the build under the test artefact directory:

- Without shared-capable tests or seed fan-out, no separate build job is submitted; each simulation job compiles in its own directory.
- A fanned-out test still gets a build job to prevent concurrent compiles into the same test directory. Workers use the stamp left by that job.
- A job that may compile uses the field-wise maximum of its simulation and compile reservations. This protects against a missing or invalid prebuilt stamp.

When a build job exists, every dependent is submitted with `--kill-on-invalid-dep=yes`. A failed build therefore removes jobs that could never satisfy `afterok`; collection also cancels any `DependencyNeverSatisfied` remnants. A user-supplied `--kill-on-invalid-dep=no` in `sbatch-args` overrides the default.

A missing result from a scheduler kill, worker crash, or dependency failure is a failed row, not a dropped test. A compile failure for one compile key does not stop unrelated keys; the affected worker retries its own compile and reports the failure.
