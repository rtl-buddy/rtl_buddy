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
        meta: {tier: pr}
```

| Field | Requirement | Meaning |
|---|---|---|
| `blocks[].name` | Required | Block identifier, matched to model name in multi-block specs |
| `blocks[].desc` | Required | Human-readable description |
| `blocks[].docs` | Optional list | Markdown paths relative to `specs.yaml` |
| `blocks[].coverage-items` | Default empty | Functional coverage item list |
| `coverage-items[].id` | Required | Identifier used by `covers` in tests and formal verifications |
| `coverage-items[].desc` | Required | Verification requirement |
| `coverage-items[].meta` | Optional mapping | Free-form project metadata, such as the test tier that owns the item. Any keys and values; rtl-buddy does not interpret it. Reported as `meta` per item by `rb --machine spec check-coverage` when set |

A key that is not listed here, at the top level, on a block or on a coverage item, is ignored. The file still loads and a warning names the file, the location and the key, once per process. A `meta:` left empty or set to `~` means no metadata. A single-block file matches its linked model unconditionally. These fields affect traceability only. See [Spec Traceability](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/spec-traceability/).
