## Configure the testbench

Add a required `toplevel:` and a `cocotb.module` string or list to the testbench entry:

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

Select the simulator as for any other test:

```bash
rb --builder icarus test cocotb_smoke
```

Unsupported simulator families and a missing `toplevel:` are fatal configuration errors. See [Tests YAML](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#testsyaml) for the complete schema and [Simulation Backends](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/simulators/) for backend differences.
