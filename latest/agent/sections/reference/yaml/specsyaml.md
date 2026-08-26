## specs.yaml

Required keys are `rtl-buddy-filetype: spec_config` and `blocks`.

```yaml
rtl-buddy-filetype: spec_config
blocks:
  - name: my_design
    desc: Design requirements
    docs: [README.md]
    coverage-items:
      - id: MY-COV-01
        desc: Normal operation
```

| Field | Requirement | Meaning |
|---|---|---|
| `blocks[].name` | Required | Block identifier matched to model name in multi-block specs |
| `blocks[].desc` | Required | Human-readable description |
| `blocks[].docs` | Optional list | Markdown paths relative to `specs.yaml` |
| `blocks[].coverage-items` | Default empty | Functional coverage item list |
| `coverage-items[].id` | Required | Identifier used by `covers` in tests and formal verifications |
| `coverage-items[].desc` | Required | Verification requirement |

A single-block file matches its linked model unconditionally. These fields affect traceability only. See [Spec Traceability](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/spec-traceability/).
