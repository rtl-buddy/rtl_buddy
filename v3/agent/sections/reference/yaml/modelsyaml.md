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
```

**Optional fields:**

| Field | Type | Description |
|-------|------|-------------|
| `desc` | string | Human-readable model description |
| `spec` | string | Path to the block's `specs.yaml`, relative to this `models.yaml` file. Used by `rb spec check-design` to link the design model to its specification. |

**Runtime effects:**

- `tests.yaml` references a model by `name` using the `model` and `model_path` fields.
- Model filelists are parsed by the filelist logic: `-F` recursion, `+incdir+`, `+libext+`, `-v`, `-y`, and plain source paths are all supported.
- `spec` is not used at simulation time; it is only consumed by the `rb spec` traceability commands.

---
