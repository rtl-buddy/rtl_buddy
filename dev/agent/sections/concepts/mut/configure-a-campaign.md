## Configure a campaign

One `mut.yaml` defines one campaign:

```yaml
rtl-buddy-filetype: mut_config

model: demo_top
model_path: ../../design/demo_top/models.yaml
design_file: ../../design/demo_top/rtl/alu.sv

operators: [arith_flip, bit_op_flip, cond_negate, cond_const]

verify:
  fpv_config: ../../fpv/demo/fpv.yaml
  verification: demo_fpv_alu_safety
  test_config: ../../verif/demo/tests.yaml
  tests: [alu_smoke, alu_random]
  assertions: true

budget:
  max_mutants: 100
  per_file_cap: null
  time_budget_minutes: null
  schedule: sequential

scope:
  include: []
  exclude: []
```

At least one kill oracle is required:

| Oracle | Required fields | A mutant is killed when |
|---|---|---|
| FPV | `fpv_config`, `verification` | The named baseline PASS becomes FAIL |
| Simulation | `test_config`; optional `tests`, `assertions` | A selected test fails or an SVA assertion fires |

When both are configured, either oracle may kill a mutant. Simulation enables Verilator assertions by default; see [Assertion-based verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/abv-simulation/).

`design_file` must be inside the directory containing `models.yaml`, because each mutant is evaluated in an isolated copy of that tree. It is also the baseline-oracle target. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/) for the full field schema.

Supported operators are:

| Operator | Mutation |
|---|---|
| `arith_flip` | Flip an arithmetic operator |
| `bit_op_flip` | Flip a bitwise or logical operator |
| `cond_negate` | Negate a condition |
| `cond_const` | Replace a condition with a constant |
| `assign_drop` | Drop an assignment |
| `port_binding_swap` | Swap two port bindings |

An empty operator list or an operator unsupported by the installed engine is fatal.
