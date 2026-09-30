---
description: Parse, type-check, and elaborate models quickly with pyslang, using optional profiles in models.yaml and explicit regression manifests.
---

# Model Elaboration

`rb elab` is a fast SystemVerilog parse, type-check, and elaboration gate that builds no simulator executable. It reads the same model and filelist as every other model-based flow. There is no `elab.yaml` and no second model path to keep in sync.

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

## Raise the parser nesting limit

If a run fails with `language constructs are too deeply nested`, set `max_parse_depth` on the profile. slang stops at 1024 nesting levels, and generated RTL can exceed that inside one expression, typically a long chain of conditional expressions or concatenations. The setting is unset by default and affects only the profile that declares it:

```yaml
    elaborations:
      - name: parse
        max_parse_depth: 8192
```

Malformed sources still fail with the same diagnostics. Set it to what the generated source needs, not to the maximum: beyond the guard, later passes can exhaust the C stack and kill the worker with no diagnostic. The accepted range is in [YAML Formats](../reference/yaml.md#elaboration-profiles), and the platform limit on nesting depth is in [Quirks & Known Issues](../known-issues.md).

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

## Outputs

Each run writes below the directory containing its `models.yaml`:

```text
artefacts/elab/<model>/<base-or-profile>/
  elab.f
  elab.log
  result.json
```

- `elab.f` is the filelist with includes unrolled and path entries made absolute.
- `result.json` records the selected top, explicit and parsed source counts, error and warning counts, `max_parse_depth` (`null` when unset), elapsed time, peak worker memory, and the pyslang version.
- A missing `prepend_sources`, `append_sources`, or `include_dirs` entry gives a `FAIL` at stage `filelist` for that profile. The command continues with the remaining profiles.
- Machine mode returns the same result payload and writes JSONL events to `rtl_buddy.log`.

## Dispatch elaboration

`rb elab` dispatches only with `--dispatch`. `rb elab-regression` also honors `cfg-dispatch.backend`. A profile's `resources` layer over `cfg-dispatch.resources`, and `cpus` is both the scheduler request and the pyslang worker thread count.

- Slurm enforces the memory and time reservations.
- Local-parallel passes `cpus` to pyslang and limits concurrency with its process pool. It does not enforce memory or time.
