## Split verilation from the C++ build

Verilation is single-threaded and the C++ build is not, so one job wastes cores during elaboration. Under `--dispatch slurm`, a suite that uses the Verilator family runs the two phases as chained jobs:

| Job | Reservation | Work |
|---|---|---|
| `rb-verilate-<hash>` | `compile.verilate` | Emits C++ sources and the Makefile, builds nothing |
| `rb-build-<hash>` | `compile` | Compiles and links what the verilate job emitted |

Simulation arrays still wait on the build job. `compile.verilate.cpus` defaults to 2; its `mem` and `time` inherit the resolved `compile` values, and `compile.parallel` applies to each phase.

- If the verilate output is missing or stale, or the Verilator lacks `--no-verilate`, the build job verilates that build itself and warns `compile.build_phase_fallback`.
- With a `shared-build-root` shared by several checkouts, only a finished, stamped build is reused across checkouts. Verilate output from another checkout whose build job has not finished is never built on. The verilate job defers to it (`compile.verilate_deferred`) without verilating or overwriting it. The build job then reuses that checkout's stamp if it has finished by then, and otherwise compiles in full (`marker-foreign`). However many checkouts race on one key, the key is verilated at most twice, and no job waits for another checkout.
- To have checkouts queue instead of race, set `compile.serialise-checkouts: true` in `cfg-dispatch.compile`. With a cache root in force, a suite's verilate and build jobs are then named after its cache directory (`<cache root>/<suite namespace>`) rather than the checkout. Every checkout sharing the cache submits under the same names, so `--dependency=singleton` holds a later checkout's jobs in the queue, with no allocation, until the earlier checkout's jobs end. The later build job then reuses the earlier stamp: one verilation and one C++ build per key. If the earlier checkout fails or is cancelled, its jobs end and the later one builds for itself. It is off by default because the name covers the whole suite: a checkout with different sources queues behind an unrelated build of the same suite. `singleton` only orders jobs of one user, so checkouts run by different users still race. The setting does nothing without a cache root or off Slurm.
- Builds that differ only in `-CFLAGS`/`-LDFLAGS` share one verilation (see [Changing only C++ compile or link flags](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/tests/#changing-only-c-compile-or-link-flags)). The verilate job verilates it once, into the build directory of the compile line without those options, and each build job copies it into its own build directory and runs only the make.
- Set `compile.split-verilate: false` in `cfg-dispatch.compile` or a suite's `compile:` block to run one build job instead.

An `OUT_OF_MEMORY` on a large design is normally a `compile.verilate.mem` edit; `compile.mem` then only needs one compiler process's memory. Size the build job's `cpus` at the Verilator `-j` or `--build-jobs` value in `builder-opts.<mode>.compile-time` times `compile.parallel`.
