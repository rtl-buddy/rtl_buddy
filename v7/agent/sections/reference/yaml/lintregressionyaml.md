## lint_regression.yaml

Required keys are `rtl-buddy-filetype: lint_reg_config` and `lint-configs`:

```yaml
rtl-buddy-filetype: lint_reg_config
lint-configs: [lint/style/lint.yaml]
```

Paths resolve from the manifest. `rb lint-regression` filters checks by `--reg-level`. Discovery checks `./lint_regression.yaml` before `cfg-rtl-reg.lint-reg-cfg-path`.
