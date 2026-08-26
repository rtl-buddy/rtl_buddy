## Enable coverage

Coverage instrumentation must be present at compile time. Add a builder mode in `root_config.yaml` and select it when running tests:

```yaml
cfg-rtl-builder:
  - name: verilator
    builder: verilator
    builder-simv: obj_dir/simv
    builder-opts:
      cov:
        compile-time: --binary -sv -o simv --coverage
        run-time: +verilator+rand+reset+2

cfg-coverage:
  - name: verilator
    use-lcov: true
```

```bash
rb -M cov test basic
rb -M cov regression
```

`cfg-coverage.name` must match the simulator family. `use-lcov: true` enables LCOV conversion and HTML generation. Configure optional Coverview packaging under `cfg-coverview`; see [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml).

Any coverage output flag asserts that executed tests will produce raw coverage. If no non-skipped test does, the command exits 2 with a configuration error. A selection containing only skipped tests does not error.
