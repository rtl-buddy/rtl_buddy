## Configure `fpv.yaml`

Each entry names a model, proof mode, and property inputs:

```yaml
rtl-buddy-filetype: fpv_config

verifications:
  - name: demo_fpv_fifo
    tool: sby
    model: demo_fifo
    model_path: ../../design/demo_fifo/models.yaml
    top: demo_fifo
    constraints: shared_clock_reset.sv
    properties: [demo_fifo_props.sv]
    mode: bmc
    depth: 32
    engines: [smtbmc yices]
    reglvl: 1000
```

Paths are relative to `fpv.yaml`. `top` defaults to the model name, `depth` to 20, and `engines` to `smtbmc yices`. `properties` may be omitted when assertions live in RTL under `` `ifdef FORMAL ``. Modes are `bmc`, `prove`, `cover`, and `live`.

Optional run controls include:

- `params` for top-level parameter overrides;
- `tool_overrides` for `timeout` or `extra_args`;
- `frontend: verilog|slang`;
- `coi` and `vacuity` analysis toggles;
- `covers` for [spec traceability](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/spec-traceability/);
- `xfail` or `xfail_strict` for [expected failures](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/expected-failures/).

See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/) for the complete schema.
