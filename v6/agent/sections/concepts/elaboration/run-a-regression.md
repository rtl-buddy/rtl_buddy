## Run a regression

An elaboration regression deliberately selects `models.yaml` files through a
small manifest. It runs only named profiles, so adding an ordinary model does
not silently expand a project-wide gate.

```yaml
rtl-buddy-filetype: elab_reg_config
model-configs:
  - design/core/models.yaml
  - design/peripherals/models.yaml
```

```bash
rb --machine elab-regression -c elab_regression.yaml --reg-level 1
```

Profiles above the requested level produce `SKIP`. A manifest with no named
profiles is an error instead of an empty passing regression. Set
`cfg-rtl-reg.elab-reg-cfg-path` when the manifest is not at the project root.
