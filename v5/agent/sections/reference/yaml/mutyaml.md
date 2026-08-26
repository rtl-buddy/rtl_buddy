## mut.yaml

Unlike the other suite configs, a `mut.yaml` describes a **single mutation campaign** (one design file under test), not a list of runs. See the [Mutation Testing concept page](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/mut/) for the full workflow.

**Required keys:**

- `rtl-buddy-filetype: mut_config`
- `model`, `model_path`, `design_file`, `operators`, `verify`

**Example:**

```yaml
rtl-buddy-filetype: mut_config

model: demo_top
model_path: "../../design/demo_top/models.yaml"
design_file: "../../design/demo_top/rtl/alu.sv"

operators:
  - arith_flip
  - bit_op_flip
  - cond_negate

verify:
  fpv_config: "../../fpv/demo/fpv.yaml"
  verification: "demo_fpv_alu_safety"
  test_config: "../../verif/demo/tests.yaml"
  tests: ["alu_smoke"]
  assertions: true

budget:
  max_mutants: 100
  schedule: "sequential"
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Model name within the referenced `models.yaml` |
| `model_path` | string | Path to the `models.yaml`, resolved relative to `mut.yaml` |
| `design_file` | string | The single SystemVerilog file to mutate, relative to `mut.yaml`. Must live within the model directory so per-mutant isolation can copy the tree |
| `operators` | list of strings | Non-empty list of operators: `arith_flip`, `bit_op_flip`, `cond_negate`, `cond_const`, `assign_drop`, `port_binding_swap`. Empty or unknown ⇒ fatal config error |
| `verify.fpv_config` | string | Path to an `fpv.yaml`, relative to `mut.yaml` (FPV kill oracle) |
| `verify.verification` | string | Verification name in that `fpv.yaml` — **required when** `fpv_config` is set |
| `verify.test_config` | string | Path to a `tests.yaml`, relative to `mut.yaml` (simulation kill oracle) |
| `verify.tests` | list of strings | Optional subset of test names; empty (default) runs every test in the suite |
| `verify.assertions` | bool | Compile SVA in via Verilator `--assert`. Default `true` |
| `name` | string | Campaign id; used in `artefacts/mut/<name>/`. Defaults to `model` |
| `top` | string | Top module under test. Defaults to `model` |
| `budget.max_mutants` | int | Cap on mutants generated. Default `100` |
| `budget.per_module_cap` | int or null | Per-module cap, or `null` (default) for none |
| `budget.time_budget_minutes` | float or null | Wall-clock budget in minutes, or `null` (default) for none |
| `budget.schedule` | string | `"sequential"` (default) or `"round_robin"` |
| `scope.include` / `scope.exclude` | list of strings | Optional include/exclude lists (no-op for single-file campaigns) |

**Runtime effects:**

- `verify` must configure at least one kill oracle (`fpv_config` + `verification`, and/or `test_config`); otherwise config load fails. When both are set, a mutant is killed if either oracle catches it.
- `rb mut run` writes `mut_report.json` under `<mut.yaml dir>/artefacts/mut/<campaign>/`. It exits `1` only when nothing was scorable; score thresholding is not gated.
- The mutation engine lives in the optional [`rtl-buddy-xeno`](https://github.com/rtl-buddy/rtl-buddy-xeno) package (`pip install "rtl-buddy-xeno[verible,slang]"`); `rb mut` raises a fatal error with this hint if it is not installed.

---
