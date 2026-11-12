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

`cfg-coverage.name` must match the simulator family. `use-lcov: true` enables LCOV conversion and HTML generation.

`--coverage` turns on every kind of Verilator coverage. A mode that lists the kinds one by one must name each metric it reports: from Verilator 5.054, `--coverage-line` covers basic blocks only, and the `if`/`else` and `?:` branch points need `--coverage-branch`. Without it, branch points are missing from `rb cov` and the coverage model, an `if`/`else` counts as two line points, and the LCOV branch figure counts only lines with more than one point. Verilator 5.052 and earlier include branch points in `--coverage-line` and reject `--coverage-branch`. Coverview packaging is configured under `cfg-coverview`; see [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v8/reference/yaml/#root_configyaml).
