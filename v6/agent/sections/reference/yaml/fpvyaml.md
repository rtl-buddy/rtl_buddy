## fpv.yaml

Required keys are `rtl-buddy-filetype: fpv_config` and `verifications`.

```yaml
rtl-buddy-filetype: fpv_config
verifications:
  - name: demo_fpv_fifo
    desc: FIFO interface properties
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

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Verification identifier and artefact directory |
| `desc` | Required | Human-readable description |
| `tool` | Required | Backend and `cfg-fpv-tools` entry; only `sby` is supported |
| `model` | Required | Model name |
| `model_path` | Required | `models.yaml` relative to `fpv.yaml` |
| `top` | Default model | Elaboration top |
| `properties` | Optional | Property files relative to `fpv.yaml`; may be omitted for in-RTL FORMAL properties |
| `constraints` | Optional | One environment-assumption file, read before properties |
| `mode` | Default `bmc` | `bmc`, `prove`, `cover`, or `live` |
| `depth` | Default 20 | Proof depth |
| `engines` | Default `[smtbmc yices]` | SymbiYosys engine specifications |
| `params` | Optional map | Top-level parameter overrides applied to proof, vacuity, and COI elaboration |
| `reglvl` | Optional | Regression level |
| `covers` | Optional list | Specification coverage IDs; no proof effect |
| `tool_overrides` | Optional map | Per-tool `timeout` and `extra_args` |
| `vacuity` | Default true for bmc/prove | Derive antecedent reachability covers; default false for cover/live |
| `coi` | Default true | Run cone-of-influence and dead-assume analysis |
| `frontend` | Default `verilog` | `verilog` or `slang`; slang requires the configured plugin |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Parameter names must be identifiers. Values may be integers, booleans, or strings containing whitespace-free SystemVerilog literal text; string parameters need embedded quotes, for example `MODE: '"small"'`. YAML boolean-like keys such as unquoted `on` and invalid values are rejected. The verilog frontend uses `chparam`; slang applies `-G` during elaboration.

Design sources, constraints, and properties are read in that order. See [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpv/) for frontend behavior, proof-quality checks, artefacts, and counterexamples.
