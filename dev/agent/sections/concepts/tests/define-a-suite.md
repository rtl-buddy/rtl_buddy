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

`model_path`, testbench filelists, and hook paths resolve from the directory containing `tests.yaml`, and a model's filelist entries resolve from the directory containing its own filelist — a `+incdir+` inside a filelist pulled in with `-F` names a directory beside that filelist, not beside the suite that consumes it, so a design filelist can carry its own include path. `plusargs` affect simulation; `plusdefines` affect compilation. See [YAML Formats: tests.yaml](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#testsyaml) for all fields and [cocotb Testbenches](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/cocotb/) for Python-driven tests.

`toplevel:` names the module the compile elaborates from and reaches the builder as Verilator `--top-module`, VCS `-top`, or Icarus `-s`. Declare it on every testbench: without it the simulator elects a top from filelist order, so recomposing a model filelist renames the Verilator model and an uninstantiated module in an ordinary (non-`-v`) input turns the build into a `MULTITOP` error. For a SystemVerilog bench it names the bench, not the DUT. It is not inferred from `name`, and a top pinned in the builder's `compile-time` opts still wins. See [Pinning the elaboration top](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#pinning-the-elaboration-top).
