---
description: Parse, type-check, and elaborate models quickly with pyslang, using optional profiles in models.yaml and explicit regression manifests.
---

# Model Elaboration

Use `rb elab` for a fast SystemVerilog parse, type-check, and elaboration gate
without building a simulator executable. It consumes the same model and
filelist every other model-based flow already uses; there is no separate
`elab.yaml` and no second model path to keep synchronized.

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

## Outputs and dispatch

Each run writes below the directory containing its `models.yaml`:

```text
artefacts/elab/<model>/<base-or-profile>/
  elab.f
  elab.log
  result.json
```

`elab.f` has unrolled includes and absolute path-valued entries. `result.json` records
the selected top, explicit and parsed source counts, error and warning counts,
elapsed time, peak worker memory, and pyslang version. Machine mode returns the
same result payload and writes JSONL events to `rtl_buddy.log`.

`rb elab` dispatch is opt-in with `--dispatch`. `rb elab-regression` also honors
`cfg-dispatch.backend`. Profiles layer their `resources` over
`cfg-dispatch.resources`; `cpus` is both the scheduler request and pyslang
worker thread count. Slurm enforces memory and time reservations.
Local-parallel passes `cpus` to pyslang and uses its process-pool limit for
concurrency, but does not enforce memory or time.
