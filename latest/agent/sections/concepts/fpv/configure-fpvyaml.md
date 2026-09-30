## Configure `fpv.yaml`

Each entry names a model, a proof mode and the property inputs:

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

- Paths are relative to `fpv.yaml`.
- Modes are `bmc`, `prove`, `cover` and `live`.
- `top` defaults to the model's root module, `depth` to 20 and `engines` to `smtbmc yices`.
- `properties` may be omitted when assertions live in RTL under `` `ifdef FORMAL ``. Both frontends define `FORMAL`.

Other fields: `params` (parameter overrides), `tool_overrides` (`timeout`, `extra_args`), `frontend`, `coi` and `vacuity` toggles, `covers` for [spec traceability](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/spec-traceability/), and `xfail` / `xfail_strict` for [expected failures](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/expected-failures/). See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/) for the schema.

Project-wide tool settings go in `root_config.yaml`:

```yaml
cfg-fpv-tools:
  - name: sby
    tool: sby
    opts:
      timeout: 600
      extra-args: ""
      solver-versions:
        yices: "2.6.4"
        z3: "4.13.0"
```

`solver-versions` pins exact versions for `yices`, `z3`, `boolector`, `bitwuzla`, `btormc` and `abc`. A run fails before starting if any pin does not match, and lists every mismatch.
