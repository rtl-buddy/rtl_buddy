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

`design_file` must be inside the directory containing `models.yaml`, because each mutant is evaluated in an isolated copy of that tree. It is also the baseline-oracle target. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/) for the full schema.
