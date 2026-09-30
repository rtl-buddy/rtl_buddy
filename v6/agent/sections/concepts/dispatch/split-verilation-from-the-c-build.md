## Split verilation from the C++ build

Verilation is single-threaded and the C++ build is not, so one job wastes cores during elaboration. Under `--dispatch slurm`, a suite that uses the Verilator family runs the two phases as chained jobs:

| Job | Reservation | Work |
|---|---|---|
| `rb-verilate-<hash>` | `compile.verilate` | Emits C++ sources and the Makefile, builds nothing |
| `rb-build-<hash>` | `compile` | Compiles and links what the verilate job emitted |

Simulation arrays still wait on the build job. `compile.verilate.cpus` defaults to 2; its `mem` and `time` inherit the resolved `compile` values, and `compile.parallel` applies to each phase.

- If the verilate output is missing or stale, or the Verilator lacks `--no-verilate`, the build job verilates that build itself and warns `compile.build_phase_fallback`.
- Set `compile.split-verilate: false` in `cfg-dispatch.compile` or a suite's `compile:` block to run one build job instead.

An `OUT_OF_MEMORY` on a large design is normally a `compile.verilate.mem` edit; `compile.mem` then only needs one compiler process's memory. Size the build job's `cpus` at the Verilator `-j` or `--build-jobs` value in `builder-opts.<mode>.compile-time` times `compile.parallel`.
