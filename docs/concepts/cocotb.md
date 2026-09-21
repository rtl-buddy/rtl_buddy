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

RTL Buddy calls `cocotb-config` during compilation and reports an installation error if it is unavailable.

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

Unsupported simulator families and a missing `toplevel:` are fatal configuration errors. `toplevel:` becomes `COCOTB_TOPLEVEL` and also roots the compile — Verilator `--top-module`, VCS `-top`, Icarus `-s` — unless the builder's `compile-time` opts already pin a top. See [Tests YAML](../reference/yaml.md#testsyaml) for the complete schema and [Simulation Backends](simulators.md) for backend differences.

## Interpret results

cocotb writes `cocotb_results.xml`. RTL Buddy parses that file automatically and reports up to the first three failure messages, plus a remaining-count suffix. Do not add transcript `PASS` or `FAIL` markers for cocotb tests.
