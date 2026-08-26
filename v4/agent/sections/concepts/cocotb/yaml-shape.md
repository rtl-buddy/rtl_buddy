## YAML shape

Add `toplevel:` and a `cocotb:` block to a testbench entry in `tests.yaml`. `toplevel:` is **required** when `cocotb:` is present — omitting it is a fatal config error caught at load time.

```yaml
testbenches:
  - name: "tb_my_design"
    filelist:
      - "my_design.sv"
    toplevel: my_design          # required: DUT top-level module name
    cocotb:
      module: test_my_design     # Python module with @cocotb.test() coroutines

  - name: "tb_multi"
    filelist:
      - "my_design.sv"
    toplevel: my_design
    cocotb:
      module:                    # list form: all modules are loaded
        - test_smoke
        - test_corner_cases

tests:
  - name: "test_my_design"
    desc: "cocotb test"
    reglvl: 0
    model: "my_design"
    model_path: "../../design/block/models.yaml"
    testbench: "tb_my_design"
```
