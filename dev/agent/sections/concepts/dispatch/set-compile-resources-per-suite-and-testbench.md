## Set compile resources per suite and testbench

`cfg-dispatch.compile` is one reservation for every suite's build job. A suite that needs something different sets `compile:` at the top level of its `tests.yaml`:

```yaml
compile:
  mem: 48G          # cpus and time inherited
  parallel: 1       # builds run at once in this suite's build job
```

A testbench can set its own `compile:` when a suite's entries verilate at different scales:

```yaml
compile:
  mem: 8G           # the small geometry, which most runs use

testbenches:
  - name: tb_chip_t1
    compile:
      mem: 256G
      time: "06:00:00"
```

- Fields resolve in this order: testbench `compile`, suite `compile`, `cfg-dispatch.compile`, `cfg-dispatch.resources`, built-in defaults.
- A testbench block wins over the suite block field by field, in both directions. Each field must be greater than zero, and `parallel` is not allowed there.
- `compile.verilate` sizes the verilate job of a split suite and `compile` sizes the C++ build alone. See [Split verilation from the C++ build](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#split-verilation-from-the-c-build). `compile.split-verilate` belongs to `cfg-dispatch.compile` or the suite block, not a testbench.

The suite block is the floor for the build job. The job aggregates the testbench blocks of the tests in the run:

- `cpus`: the largest block, times `compile.parallel`.
- `mem`: the sum of the largest `min(parallel, n)` builds, since concurrent builds each hold their own peak.
- `time`: the finish time of the job's work queue, where each build starts on the first free of `parallel` workers in plan order. Builds of 30, 30, and 20 minutes on two workers take 50 minutes.

Only testbenches with selected tests count, so a run that skips `tb_chip_t1` reserves the suite's `8G`. The head counts one build per distinct testbench, plusdefines, builder, model and `assertions`, except that a test with a `preproc` hook counts as its own build, because the hook may set plusdefines. Set `preproc-sets-plusdefines: false` on the test or the suite when the hook only writes stimulus, so twenty such tests on one testbench reserve one build, not twenty. With no testbench `compile:` block, suite `compile.mem` is the whole job, so size it for `parallel` concurrent builds from elaboration, not simulation. Once any testbench states its own `mem`, the suite value is read as one build's peak for each build that has none. A 256G testbench beside an unannotated build under `compile: {mem: 8G, parallel: 2}` reserves 264G.
