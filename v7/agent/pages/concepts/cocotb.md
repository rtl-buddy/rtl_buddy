---
description: Configure cocotb testbenches for Verilator, Icarus Verilog, or VCS and interpret their results.
---

# cocotb Testbenches

RTL Buddy runs cocotb tests through VPI on builders whose simulator family is `verilator`, `icarus`, or `vcs`.

## Install cocotb

Install cocotb in the same environment as RTL Buddy:

```bash
uv add cocotb
```

RTL Buddy calls `cocotb-config` at compile time and reports an installation error if it is missing.

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

See [Tests YAML](../reference/yaml.md#testsyaml) for the schema and [Simulation Backends](simulators.md) for backend differences.

## Interpret results

cocotb writes `cocotb_results.xml`, which RTL Buddy parses. The result reports up to the first three failure messages plus a count of the rest. Do not add transcript `PASS` or `FAIL` markers to cocotb tests.
