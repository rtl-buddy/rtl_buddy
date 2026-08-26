## FPV config: `fpv.yaml`

`fpv.yaml` declares one or more verification runs. Each entry references a model from `models.yaml`, a top module (defaults to the model name), and one or more property files:

```yaml
rtl-buddy-filetype: fpv_config

verifications:
  - name: "demo_fpv_fifo"
    desc: "Bounded proof of FIFO interface assertions"
    tool: "sby"
    model: "demo_fifo"
    model_path: "../../design/demo_fifo/models.yaml"
    top: "demo_fifo"
    constraints: "shared_clock_reset.sv"     # optional
    properties:
      - "demo_fifo_props.sv"
    mode: "bmc"
    depth: 32
    engines:
      - "smtbmc yices"
    reglvl: 1000
```

### Fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — only `"sby"` is supported today |
| `model` | Model name from `models.yaml` |
| `model_path` | Path to `models.yaml`, resolved relative to `fpv.yaml` |
| `top` | Top module passed to `prep -top`; defaults to `model` when omitted |
| `properties` | List of `.sv` files containing SVA properties / bound checker modules, resolved relative to `fpv.yaml`. Optional when properties are in-RTL under `` `ifdef FORMAL `` guards |
| `constraints` | Optional path to a single `.sv` file containing environment `assume property` statements (clock toggle, reset sequence, etc.) — analogous to `constraints:` in `pnr.yaml`. Read into the sby script *before* `properties:` so the assumes are in scope when the asserts elaborate. Lets multiple verifications share one clock/reset boilerplate file instead of duplicating it across every bound checker. |
| `mode` | One of `bmc` (bounded), `prove` (k-induction), `cover`, `live` |
| `depth` | Cycle depth passed to sby; defaults to 20 |
| `engines` | List of sby engine specs (e.g. `smtbmc yices`, `abc pdr`); defaults to `["smtbmc yices"]` |
| `reglvl` | Regression level for filtering (same semantics as `rb synth` / `rb cdc`) |
| `tool_overrides` | Optional per-tool overrides for `timeout` or `extra_args`, keyed by FPV tool name |
| `vacuity` | Optional bool. When true (default for `bmc` / `prove`), run a secondary sby cover-mode pass over auto-derived cover properties for every `a \|-> b` antecedent in the property set — flags vacuous proofs. Defaults to false for `cover` / `live` modes. See [Vacuity covers](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/#vacuity-covers). |
| `coi` | Optional bool. When true (default), run a yosys cone-of-influence pass and report the fraction of design cells reachable from at least one assertion. See [Cone-of-influence coverage](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/#cone-of-influence-coverage). |
| `frontend` | SystemVerilog frontend: `"verilog"` (default — fast, no plugin, immediate-assert + simple-concurrent SVA only) or `"slang"` (yosys-slang plugin — required for concurrent SVA `\|->` / `\|=>` / sequence operators and for `bind` to elaborate). `slang` requires `cfg-fpv-tools[].opts.plugin-path` in root_config.yaml. See [Choosing a frontend](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/#choosing-a-frontend). |

### Where inputs come from

The runner reads the model's filelist via `VlogFilelist` (the same helper `rb synth` and `rb cdc` use), extracts source files and `+incdir+` entries, and emits them under the sby config's `[files]` and `[script]` sections respectively. The script reads, in order: design sources → `constraints:` (if set) → `properties:`. Putting constraints before properties ensures their `assume property` statements are in scope when the assertions are elaborated. Property files can be in-RTL with `` `ifdef FORMAL `` guards or standalone bound checker modules.
