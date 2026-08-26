## Mutation config: `mut.yaml`

```yaml
rtl-buddy-filetype: mut_config

model: demo_top
model_path: "../../design/demo_top/models.yaml"
design_file: "../../design/demo_top/rtl/alu.sv"

operators:
  - arith_flip
  - bit_op_flip
  - cond_negate
  - cond_const

verify:
  # FPV oracle (fpv_config requires verification)
  fpv_config: "../../fpv/demo/fpv.yaml"
  verification: "demo_fpv_alu_safety"
  # Simulation oracle (optional; combine with or use instead of FPV)
  test_config: "../../verif/demo/tests.yaml"
  tests: ["alu_smoke", "alu_random"]   # empty/omitted = every test in the suite
  assertions: true

budget:
  max_mutants: 100
  per_module_cap: null
  time_budget_minutes: null
  schedule: "sequential"
```

### Fields

| Field | Description |
|---|---|
| `model` | Model name within the referenced `models.yaml` |
| `model_path` | Path to the `models.yaml`, resolved relative to `mut.yaml` |
| `design_file` | The single SystemVerilog file to mutate, resolved relative to `mut.yaml`. **Must live within the model directory** (the directory containing `models.yaml`) so per-mutant isolation can copy the tree and splice the mutant in |
| `operators` | Non-empty list of mutation operators (see below). An empty list or an unknown operator is a fatal config error |
| `verify` | The kill-oracle block — at least one oracle required (see [Kill oracles](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/mut/#kill-oracles)) |
| `verify.fpv_config` | Path to an `fpv.yaml`, relative to `mut.yaml` (FPV oracle) |
| `verify.verification` | Name of the verification in that `fpv.yaml` to use as the oracle — **required when** `fpv_config` is set |
| `verify.test_config` | Path to a `tests.yaml`, relative to `mut.yaml` (simulation oracle) |
| `verify.tests` | Optional subset of test names to run; empty (default) runs every test in the suite |
| `verify.assertions` | Compile SVA in via Verilator `--assert`. Default `true` |
| `name` | Campaign identifier; used in `artefacts/mut/<name>/`. Defaults to `model` |
| `top` | Top module under test. Defaults to `model` |
| `budget.max_mutants` | Cap on the number of mutants generated. Default `100` |
| `budget.per_module_cap` | Per-module cap, or `null` for none (default `null`) |
| `budget.time_budget_minutes` | Wall-clock budget in minutes, or `null` for none (default `null`) |
| `budget.schedule` | `"sequential"` (default) or `"round_robin"` |
| `scope.include` / `scope.exclude` | Optional include/exclude lists (no-op for single-file leaf campaigns) |

### Operators

The six operators map 1:1 onto `rtl_buddy_xeno.MutationKind`. An operator not recognised by the *installed* `rtl-buddy-xeno` is a fatal error at run time:

| Operator | Mutation |
|---|---|
| `arith_flip` | Flip an arithmetic operator (e.g. `+` ↔ `-`) |
| `bit_op_flip` | Flip a bitwise/logical operator (e.g. `&` ↔ `\|`) |
| `cond_negate` | Negate a condition |
| `cond_const` | Force a condition to a constant |
| `assign_drop` | Drop an assignment |
| `port_binding_swap` | Swap two port bindings on an instantiation |
