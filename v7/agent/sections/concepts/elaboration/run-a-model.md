## Run a model

Install the optional Python frontend, list the models, then run one:

```bash
uv add "rtl_buddy[elab]"
rb --machine elab --list -c design/models.yaml
rb --machine elab core -c design/models.yaml
```

A bare run uses the model's `filelist` and elaborates `model.top`, or the model name if `top` is unset. Add a named profile only when a gate needs different sources, defines, parameters, compatibility options, resources, or top:

```yaml
rtl-buddy-filetype: model_config
models:
  - name: core
    top: core_top
    filelist: [-F core.f]
    elaborations:
      - name: smoke
        append_sources: [checks/bind_checks.sv]
        defines: {CHECKS_ENABLED: 1}
        parameters: {DATA_WIDTH: 32}
        warnings: [all]
        reglvl: 0
        resources: {cpus: 2, mem: 2G, time: "00:10:00"}
```

```bash
rb --machine elab core --profile smoke -c design/models.yaml
```

- A profile `top` overrides the model `top`, which overrides the model name.
- Profile source and include paths resolve from `models.yaml`.
- `warnings` entries are the text after `-W`. They can suppress warnings but not hard parse, type, or elaboration errors.
