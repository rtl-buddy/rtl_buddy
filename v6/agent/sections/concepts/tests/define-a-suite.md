## Define a suite

```yaml
rtl-buddy-filetype: test_config

testbenches:
  - name: tb_top
    toplevel: tb_top
    filelist:
      - +incdir+../../../verif/tb
      - tb_top.sv

tests:
  - name: smoke
    desc: sanity test
    reglvl: 0
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    plusargs:
      test_cycles: 50
    plusdefines:
      FEATURE_X: 1
    sim_timeout: 120
```

- `plusargs` are runtime arguments. `plusdefines` are compile-time defines.
- `model_path`, testbench filelists and hook paths resolve from the directory containing `tests.yaml`. A model's filelist entries resolve from the directory containing that filelist.
- `toplevel` names the module to elaborate: Verilator `--top-module`, VCS `-top`, Icarus `-s`. Declare it on every testbench, naming the bench and not the DUT. Without it the simulator picks a top from filelist order, and recomposing a filelist can rename the model or raise a `MULTITOP` error. A top pinned in the builder's `compile-time` opts wins; see [Pinning the elaboration top](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#pinning-the-elaboration-top).

See [YAML Formats: tests.yaml](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#testsyaml) for all fields and [cocotb Testbenches](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/cocotb/) for Python-driven tests.
