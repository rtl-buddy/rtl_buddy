---
description: How to setup rtl-buddy tests to use cocotb. How are cocotb results reported.
---

# cocotb testbenches (Verilator/VCS + VPI)

Explains how `rtl_buddy` integrates [cocotb](https://www.cocotb.org) — a coroutine-based Python verification framework — to drive RTL via simulator's VPI, covering YAML configuration, pass/fail detection, and prerequisites. Both verilator and VCS VPI flows are supported.

## Prerequisites

`cocotb` must be installed in the active Python environment:

```bash
uv add cocotb
# or: pip install cocotb
```

The runner calls `cocotb-config` at compile time. If it is missing, `rtl_buddy` raises a `FatalRtlBuddyError` with an actionable message rather than a raw traceback.

Supported simulators: **Verilator** (macOS + Linux) and **VCS** (Linux). The correct VPI library and compile/runtime flags are selected automatically based on the configured builder.

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

## Pass/fail detection

cocotb writes a JUnit XML results file (`cocotb_results.xml`) instead of `PASS`/`FAIL` stdout lines. `rtl_buddy` parses this file automatically — do **not** add `$display("PASS …")` in cocotb tests. The `desc` field reports the first three failure messages with a `(+N more)` suffix when there are more.

## Testbench field reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `toplevel` | string | Yes (cocotb only) | Top-level DUT module name → `COCOTB_TOPLEVEL` |
| `cocotb.module` | string or list | Yes | Python test module(s) → `COCOTB_TEST_MODULES` |

See `rtl-buddy docs show reference/yaml` for the full `tests.yaml` schema.
