## Run a regression

An elaboration regression takes a small manifest that lists `models.yaml` files. It runs only named profiles, so adding an ordinary model does not widen a project-wide gate.

```yaml
rtl-buddy-filetype: elab_reg_config
model-configs:
  - design/core/models.yaml
  - design/peripherals/models.yaml
```

```bash
rb --machine elab-regression -c elab_regression.yaml --reg-level 1
```

- Profiles above the requested level report `SKIP`.
- A manifest with no named profiles is an error, not an empty pass.
- If the manifest is not at the project root, set `cfg-rtl-reg.elab-reg-cfg-path`.
