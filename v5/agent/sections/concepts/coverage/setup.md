## Setup

### Builder mode

Coverage instrumentation requires a builder mode that adds coverage flags at compile time. Add a `cov` mode (or similar name) to your builder entry in `root_config.yaml`:

```yaml
cfg-rtl-builder:
  - name: "verilator"
    builder: "verilator"
    builder-simv: "obj_dir/simv"
    builder-opts:
      cov:
        compile-time: >-
          --binary -sv -o simv
          --coverage
        run-time: "+verilator+rand+reset+2"
```

Run with the coverage builder mode:

```bash
rtl-buddy --builder-mode cov test basic
rtl-buddy --builder-mode cov regression
```

### Coverage config in root_config.yaml

The `cfg-coverage` section tells `rtl_buddy` how to post-process coverage output for each simulator family:

```yaml
cfg-coverage:
  - name: "verilator"
    use-lcov: true
```

`use-lcov: true` enables `.info` file export and LCOV HTML generation when `--coverage-html` is used. The `name` field must match the simulator family name used in `cfg-rtl-builder`.

### Coverview config in root_config.yaml

The optional `cfg-coverview` section configures Coverview packaging:

```yaml
cfg-coverview:
  - name: "verilator"
    generate-tables: "line"
    config:
      # inline Coverview JSON configuration values
```

Fields:

- `name`: simulator family name, matching `cfg-rtl-builder`
- `generate-tables`: coverage type to use for Coverview tables (e.g. `"line"`)
- `config`: inline dict of Coverview JSON configuration values
