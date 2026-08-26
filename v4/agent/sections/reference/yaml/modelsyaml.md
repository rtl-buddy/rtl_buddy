## models.yaml

**Required keys:**

- `rtl-buddy-filetype: model_config`
- `models`

**Example:**

```yaml
rtl-buddy-filetype: model_config

models:
  - name: "my_design"
    desc: "Optional human-readable description"
    filelist:
      - "-F my_design.f"
    spec: "../../spec/my_design/specs.yaml"
    cdc:   "../../cdc/my_design/cdc.yaml"
    synth: "../../synth/my_design/synth.yaml#fast"
    tests: "../../verif/my_design/tests.yaml"
```

**Optional fields:**

| Field | Type | Description |
|-------|------|-------------|
| `desc` | string | Human-readable model description |
| `spec` | string | Path to the block's `specs.yaml`, relative to this `models.yaml` file. Used by `rb spec check-design` to link the design model to its specification. |
| `cdc` | string | Path to the `cdc.yaml` that owns this model's CDC analysis, relative to this `models.yaml`. Optional `#analysis_name` fragment picks one analysis from a multi-analysis file (e.g. `cdc.yaml#full_design`). Read by `rb hub` to enable the clock-domain overlay; absent → overlay unavailable. |
| `synth` | string | Path to the `synth.yaml` that owns this model's synthesis flow, relative to this `models.yaml`. Same `#synth_name` fragment semantics. Declared now for forward compatibility; no consumer reads it yet. |
| `tests` | string | Path to the `tests.yaml` that owns this model's testbench/test suite, relative to this `models.yaml`. Same `#test_name` fragment semantics. Declared now for forward compatibility; no consumer reads it yet. |

**Runtime effects:**

- `tests.yaml` references a model by `name` using the `model` and `model_path` fields.
- Model filelists are parsed by the filelist logic: `-F` recursion, `+incdir+`, `+libext+`, `-v`, `-y`, and plain source paths are all supported.
- `spec` is not used at simulation time; it is only consumed by the `rb spec` traceability commands.
- `cdc` / `synth` / `tests` are *back-pointers* — the downstream files still carry their own `model:` + `model_path:` references back to this one. The model-side entry is the source of truth for "which analysis owns this model" when there could otherwise be ambiguity (e.g. two `cdc.yaml` files reference the same model name).

---
