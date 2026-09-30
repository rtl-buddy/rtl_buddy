## Enable coverage

Coverage must be compiled in. Add a builder mode and a `cfg-coverage` entry to `root_config.yaml`, then select the mode with `-M`:

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

`cfg-coverage.name` must match the simulator family. `use-lcov: true` enables LCOV conversion and HTML generation. Coverview packaging is configured under `cfg-coverview`; see [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml).
