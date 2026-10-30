## Compile several builds at once

The build job compiles one build at a time by default. `compile.parallel` raises that to N concurrent builds. Set it in `cfg-dispatch.compile` or a suite's `compile:` block, which wins. It must be at least 1.

- Only `cpus` is scaled by `parallel`. Keep the total within the widest node of the partition.
- `compile.time` should cover the longest batch, not one build. `ceil(distinct builds / N)` times the slowest build is a safe upper bound.
- A suite that compiles one build sets `parallel: 1` so it does not reserve `cpus` times the cluster-wide value.
- At `parallel: 1`, each config runs `preproc` and then compiles before the next starts. Above 1, `preproc` runs config by config while builds compile: a build starts as soon as the first config that resolves to it is through `preproc`. Hooks must not modify another config's inputs.
- Above 1, a `preproc` hook that writes a file a build reads must leave the file untouched when its content would not change, and write a changed file atomically (to a temporary file, then rename it over the target). A build whose inputs were rewritten while it compiled, even with the same bytes, discards that build and compiles again once every `preproc` has run (`build_job.compile_inputs_moved`), which costs one extra compile of that build.
- Configs that resolve to the same build are compiled once. A config whose inputs differ from that build fails with `build_job.group_input_drift`.
