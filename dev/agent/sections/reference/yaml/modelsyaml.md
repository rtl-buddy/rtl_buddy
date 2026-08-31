## models.yaml

Required keys are `rtl-buddy-filetype: model_config` and `models`.

```yaml
rtl-buddy-filetype: model_config
models:
  - name: my_design
    filelist: [-F my_design.f]
    spec: ../../spec/my_design/specs.yaml
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Model identifier |
| `filelist` | Required | Filelist entries resolved from `models.yaml` |
| `desc` | Required | Human-readable description |
| `spec` | Optional | `specs.yaml` path for `rb spec`; no simulation effect |
| `synth` | Optional | Synthesis ownership pointer, optionally with `#entry`; no current runtime consumer |
| `tests` | Optional | Test-suite ownership pointer, optionally with `#entry`; no current runtime consumer |

Filelists support `-F` recursion, `+incdir+`, `+libext+`, `+define+`, `-v`, `-y`, and source paths. Every path-valued entry, including `+incdir+` and `-y` search directories, resolves against the directory of the filelist that declares it, so a filelist pulled in with `-F` can carry the include path its own sources need. Only the simulation flow acts on that include path: the synthesis, CDC, and FPGA flows drop `+incdir+` entries when they read the generated filelist back, so a header those flows must see needs a search path configured for them instead (see [FPGA Implementation](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpga/)). `+define+NAME[=VALUE]` is passed as a preprocessor definition; renderer-only flows drop definitions. Multiple definitions may share one entry with `+` separators, so a value cannot contain `+`. Environment variables in entries are expanded.
