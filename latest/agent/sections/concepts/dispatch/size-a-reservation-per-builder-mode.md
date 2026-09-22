## Size a reservation per builder mode

A `modes:` sub-block sizes the same test for the mode it runs in. An instrumented `-M cov` build carries per-point counters through the whole design and a `-M debug` build dumps waves, so a simulation that fits in 1 GB under `-M reg` can need an order of magnitude more memory and about twice the wall clock. Without it a suite has to commit either the coverage figure — every pull request then reserves coverage-sized memory for an uninstrumented build — or a second copy of `tests.yaml`.

Every reservation block takes one: `cfg-dispatch.resources`, `cfg-dispatch.compile`, a suite's top-level `compile:`, and a testbench's or test's `resources:` and `compile:`.

```yaml
resources:
  cpus: 1
  mem: 1G
  time: "00:15:00"
  modes:
    cov: {mem: 16G, time: "00:30:00"}
    debug: {mem: 4G}
```

The base value resolves exactly as it does without the key — most specific layer wins, field by field — and the block for the run's mode is then applied over that result, least specific layer first:

```text
test.modes[m] > testbench.modes[m] > cfg-dispatch.modes[m]
    > test > testbench > cfg-dispatch > built-in default
```

So any mode block beats every base field, not only the one on its own layer: a suite-wide `modes.cov.mem` is not undone by one test's base `mem`. A field a mode block omits keeps its base value, and a run whose mode names no block reserves exactly what it reserved before the key existed. The mode is the effective `--builder-mode` of the run — `debug` for `rb test` and `reg` for `rb regression` where the flag is absent — and it is the same value the job carries to the compute node, so a reservation and the build it sizes can never disagree.

Mode names are your own `cfg-rtl-builder.builder-opts` keys; any string is accepted, and nothing checks whether the mode exists. Quote a name YAML 1.1 reads as a boolean (`on`, `no`, `yes`).

A mode block carries the reservation fields only — `cpus`, `mem`, `time`, plus `verilate` inside a `compile:` block. `parallel`, `split-verilate`, a nested `modes:`, and any unrecognized key are rejected when the config loads rather than dropped, unlike the base fields of a `resources:` block, where an unknown key is still discarded silently. `modes:` is not accepted in `compile.verilate` (write `compile.modes.<mode>.verilate`) or on an elaboration profile, which has no builder mode to resolve.

For the compile the block layers the same way, over `cfg-dispatch.compile` and the suite and testbench blocks, and the build job's aggregation then works from what each build reserves in this mode. A coverage build is its own compile key, so its verilate peak is its own figure:

```yaml
compile:
  mem: 8G
  modes:
    cov:
      mem: 32G
      verilate: {mem: 48G}
```

Like the rest of the block, `modes:` is a scheduling fact only: it is not part of the compile fingerprint, so adding or changing one never invalidates a shared build stamp. Reservation advice names the key that governed the run, so under `-M cov` an `edit_hint.path` reads `tests[name=...].resources.modes.cov.mem` or `compile.modes.cov.mem` where a mode block supplied the value.
