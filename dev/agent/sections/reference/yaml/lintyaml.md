## lint.yaml

Required keys are `rtl-buddy-filetype: lint_config` and `checks`.

```yaml
rtl-buddy-filetype: lint_config
checks:
  - name: demo_style
    desc: Project style policy
    model: demo_top
    model_path: ../../design/demo/models.yaml
    exclude: ["*_csr_pkg.sv"]
    reglvl: 0
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Check identifier and artefact directory |
| `model` | Required | Model whose sources are linted |
| `model_path` | Required | `models.yaml` relative to `lint.yaml` |
| `desc` | Required | Human-readable description |
| `exclude` | Optional list | Additional `fnmatch` globs; `*` may cross `/` |
| `extra_args` | Optional list | Appended after `cfg-verible.extra_args.lint`; later duplicate flags win |
| `reglvl` | Optional | Regression level |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Lint uses the platform-routed `cfg-verible` entry. Model expansion drops `-v`, `-y`, and `+` directives, then applies root and check exclusions. Outputs are `artefacts/<name>/lint.f` and `lint.log`. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/) for commands and options.
