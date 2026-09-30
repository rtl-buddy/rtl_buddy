## Compile several builds at once

The build job compiles one build at a time by default. `compile.parallel` raises that to N concurrent builds. Set it in `cfg-dispatch.compile` or a suite's `compile:` block, which wins. It must be at least 1.

- Only `cpus` is scaled by `parallel`. Keep the total within the widest node of the partition.
- `compile.time` should cover the longest batch, not one build. `ceil(distinct builds / N)` times the slowest build is a safe upper bound.
- A suite that compiles one build sets `parallel: 1` so it does not reserve `cpus` times the cluster-wide value.
- At `parallel: 1`, each config runs `preproc` and then compiles before the next starts. Above 1, every config's `preproc` runs first, then the builds overlap, so hooks must not modify another config's inputs.
- Configs that resolve to the same build are compiled once. A config whose inputs differ from that build fails with `build_job.group_input_drift`.
