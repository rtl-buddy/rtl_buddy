## models.yaml

**Required keys:**

- `rtl-buddy-filetype: model_config`
- `models`

**Example:**

```yaml
rtl-buddy-filetype: model_config

models:
  - name: "my_design"
    filelist:
      - "-F my_design.f"
```

**Runtime effects:**

- `tests.yaml` references a model by `name` using the `model` and `model_path` fields.
- Model filelists are parsed by the filelist logic: `-F` recursion, `+incdir+`, `+libext+`, `-v`, `-y`, and plain source paths are all supported.

---
