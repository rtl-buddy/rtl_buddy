## mut.yaml

Required keys are `rtl-buddy-filetype: mut_config`, `model`, `model_path`, `design_file`, `operators`, and `verify`.

```yaml
rtl-buddy-filetype: mut_config
model: demo_top
model_path: ../../design/demo_top/models.yaml
design_file: ../../design/demo_top/rtl/alu.sv
operators: [arith_flip, bit_op_flip, cond_negate]
verify:
  fpv_config: ../../fpv/demo/fpv.yaml
  verification: demo_fpv_alu_safety
budget:
  max_mutants: 100
  schedule: sequential
```

| Field | Requirement | Meaning |
|---|---|---|
| `model` | Required | Model name |
| `model_path` | Required | `models.yaml` relative to `mut.yaml` |
| `design_file` | Required | Baseline mutation file inside the model directory |
| `operators` | Required, non-empty | `arith_flip`, `bit_op_flip`, `cond_negate`, `cond_const`, `assign_drop`, `port_binding_swap` |
| `verify.fpv_config` / `.verification` | Pair | FPV oracle config and entry |
| `verify.test_config` | Optional | Simulation oracle suite |
| `verify.tests` | Default all | Selected simulation tests |
| `verify.assertions` | Default true | Enables Verilator assertions for the simulation oracle |
| `name` | Default model | Campaign and artefact name |
| `top` | Default model | Top module the FPV oracle elaborates. Follows the [same rule](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#model-top) as a model `top` |
| `budget.max_mutants` | Default 100 | Global campaign cap |
| `budget.per_file_cap` | Default null | Per-scoped-file cap |
| `budget.time_budget_minutes` | Default null | Wall-clock cap |
| `budget.schedule` | Default `sequential` | `sequential` or `round_robin` |
| `scope.include` / `.exclude` | Default empty | Case-sensitive `fnmatch` globs over instance and source paths; `**` is not recursive |

At least one oracle is required, and `fpv_config` requires `verification`. `design_file` and every scoped file must stay within the model directory.

- An empty scope mutates `design_file` without the viewer.
- A non-empty scope requires `rtl-buddy-view`, selects hierarchy source files, and fails if none match.
- A campaign's `top` wins over the model's and the oracle verification's. It applies to the baseline proof and every mutant proof, since their verdicts are comparable only when elaborated from the same root.
- Only the FPV oracle elaborates a top. A campaign that configures only the simulation oracle logs `mut_config.top_override_unused` and ignores `top`.

See [Mutation Testing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/mut/).
