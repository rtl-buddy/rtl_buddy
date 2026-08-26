## Configure a regression

```yaml
rtl-buddy-filetype: reg_config

test-configs:
  - design/block_a/verif/tests.yaml
  - design/block_b/verif/tests.yaml
```

Paths resolve from the directory containing `regression.yaml`. Each suite keeps its own artefacts and detailed log; the manifest directory receives the regression log and merged outputs.

See [YAML Formats: regression.yaml](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#regressionyaml) for the schema.
