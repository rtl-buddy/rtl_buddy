## Configure the testbench

Give the testbench entry a required `toplevel:` and a `cocotb.module` string or list:

```yaml
testbenches:
  - name: tb_my_design
    filelist:
      - my_design.sv
    toplevel: my_design
    cocotb:
      module:
        - test_smoke
        - test_corner_cases

tests:
  - name: cocotb_smoke
    model: my_design
    model_path: ../../design/block/models.yaml
    testbench: tb_my_design
    reglvl: 0
```

Choose the simulator as for any test:

```bash
rb --builder icarus test cocotb_smoke
```

- An unsupported simulator family or a missing `toplevel:` is a fatal configuration error.
- `toplevel:` becomes `COCOTB_TOPLEVEL` and also sets the compile top (Verilator `--top-module`, VCS `-top`, Icarus `-s`), unless the builder's `compile-time` options already set one.

See [Tests YAML](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#testsyaml) for the schema and [Simulation Backends](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/simulators/) for backend differences.
