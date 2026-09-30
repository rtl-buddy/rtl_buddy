## Read advice for the build and verilate jobs

The build job gets a row named `(build job)` with `phase: compile`, and a split suite also gets `(verilate job)` with `phase: verilate`. Both suggest `time` in either direction and `cpus` only downward.

- The `cpus` suggestion appears only when the job ran one build at a time. With `compile.parallel` above 1, size `parallel` first, then read `cpus` from a `parallel: 1` run.
- A `reduce` is withheld when every build was reused, when the head has no per-build records, or when the job ended within one accounting interval. `rightsize.build_advice_withheld` records why. `raise` advice is unaffected.
- When the value is a sum of several builds, or a figure that two builds or the whole-job floor produce equally, no single entry can lower it, so `reduce` is withheld. A `raise` names the contributing testbench and translates the total into that entry's own new value.
- `edit_hint` names the file holding the winning value: `compile.<field>` in the suite's `tests.yaml` when the suite set it, otherwise `cfg-dispatch.compile.<field>` in `root_config.yaml`. Verilate paths add `.verilate`.
