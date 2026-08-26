## specs.yaml

`specs.yaml` lives in `spec/<block>/` and defines the functional specification for one or more design blocks. It is consumed by the `rb spec` traceability commands and has no effect on simulation.

**Required keys:**

- `rtl-buddy-filetype: spec_config`
- `blocks`

**Example:**

```yaml
rtl-buddy-filetype: spec_config

blocks:
  - name: "my_design"
    desc: "Brief description of the block"
    docs:
      - "README.md"
      - "behavior.md"
    coverage-items:
      - id: "MY-COV-01"
        desc: "Normal operation path"
      - id: "MY-COV-02"
        desc: "Error handling and recovery"
```

**Block field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Block identifier; matched against `ModelConfig.name` when resolving `spec:` links in `models.yaml`. For single-block files the name is matched unconditionally. |
| `desc` | string | Human-readable block description |
| `docs` | list of strings | Paths to markdown spec documents, relative to this `specs.yaml` file |
| `coverage-items` | list | Functional coverage items for this block |

**Coverage item fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique coverage item identifier, referenced by `covers` in `tests.yaml` |
| `desc` | string | Human-readable description of what must be tested |

See [Spec Traceability](https://rtl-buddy.github.io/rtl_buddy/v3/concepts/spec-traceability/) for the end-to-end workflow.

---
