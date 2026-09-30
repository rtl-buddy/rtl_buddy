## elab_regression.yaml

The manifest lists model configuration files and runs every named profile they contain. Bare models are not run as implicit profiles.

```yaml
rtl-buddy-filetype: elab_reg_config
model-configs:
  - design/core/models.yaml
  - design/peripherals/models.yaml
```

- `model-configs` must be non-empty, and its paths resolve from the manifest.
- Duplicate paths, and profiles that would share an artifact directory, are rejected. Path comparison is case-insensitive.
- The selected files must contain at least one profile.
- `rb elab-regression` applies `--reg-level` and records higher-level profiles as `SKIP`.
- Discovery checks `./elab_regression.yaml` before `cfg-rtl-reg.elab-reg-cfg-path`.
