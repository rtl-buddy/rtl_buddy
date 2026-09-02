## Run a model

Install the optional Python frontend, inspect the available models, then run
one:

```bash
uv add "rtl_buddy[elab]"
rb --machine elab --list -c design/models.yaml
rb --machine elab core -c design/models.yaml
```

A bare run uses the model's `filelist` and selects `model.top`, falling back to
the model name. Add a named profile only when a gate needs different sources,
defines, parameters, compatibility options, resources, or top:

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

Run it with:

```bash
rb --machine elab core --profile smoke -c design/models.yaml
```

Profile `top` overrides model `top`; model `top` overrides the model name.
Profile source and include paths resolve from `models.yaml`. Warning controls
contain only the text after `-W`; they can suppress warnings but cannot disable
hard parse, type, or elaboration errors.
