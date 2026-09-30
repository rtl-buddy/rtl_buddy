## Size a reservation per builder mode

A `modes:` sub-block sizes the same test for the builder mode it runs in. A `-M cov` build carries coverage counters and a `-M debug` build dumps waves, so a test that fits in 1 GB under `-M reg` can need far more memory and about twice the wall clock. Every reservation block takes one: `cfg-dispatch.resources` and `.compile`, a suite's `compile:`, and a testbench's or test's `resources:` and `compile:`.

```yaml
resources:
  cpus: 1
  mem: 1G
  time: "00:15:00"
  modes:
    cov: {mem: 16G, time: "00:30:00"}
    debug: {mem: 4G}
```

- Any mode block beats every base field. Layers resolve most specific first: `test.modes[m] > testbench.modes[m] > cfg-dispatch.modes[m] > test > testbench > cfg-dispatch > built-in default`. A field a mode block omits keeps its base value.
- The mode is the effective `--builder-mode`: `debug` for `rb test` and `reg` for `rb regression` when the flag is absent.
- Mode names are your own `cfg-rtl-builder.builder-opts` keys and are not checked. Quote a name that YAML reads as a boolean (`on`, `no`).
- A mode block takes `cpus`, `mem`, and `time`, plus `verilate` inside `compile:` (`compile.modes.cov.verilate`). Other keys are rejected at load.
