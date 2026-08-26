## fpv.yaml

**Required keys:**

- `rtl-buddy-filetype: fpv_config`
- `verifications`

**Example:**

```yaml
rtl-buddy-filetype: fpv_config

verifications:
  - name: "demo_fpv_fifo"
    desc: "Bounded proof of FIFO interface assertions"
    tool: "sby"
    model: "demo_fifo"
    model_path: "../../design/demo_fifo/models.yaml"
    top: "demo_fifo"
    constraints: "shared_clock_reset.sv"   # optional environment assumes
    properties:
      - "demo_fifo_props.sv"
    mode: "bmc"
    depth: 32
    engines:
      - "smtbmc yices"
    reglvl: 1000

  - name: "alu_accel_fpv"
    desc: "k-induction prove of ALU accelerator invariants"
    tool: "sby"
    model: "alu_accel_top"
    model_path: "../../design/alu_accel/models.yaml"
    properties:
      - "alu_accel_props.sv"
    mode: "prove"
    depth: 16
    engines:
      - "smtbmc z3"
      - "abc pdr"
    reglvl:
      default: 0
      sby: 1000
    tool_overrides:
      sby:
        timeout: 1800
        extra_args: ""
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Verification identifier; used on the CLI and in `artefacts/{name}/` |
| `desc` | string | Human-readable verification description |
| `tool` | string | FPV tool name from `root_config.yaml` `cfg-fpv-tools` |
| `model` | string | Model name from `models.yaml` |
| `model_path` | string | Path to `models.yaml`, resolved relative to the `fpv.yaml` file |
| `top` | string | Top module name passed to `prep -top`; defaults to `model` |
| `properties` | list | SystemVerilog files containing SVA properties / bound checkers, resolved relative to `fpv.yaml`. Optional when properties are in-RTL under `` `ifdef FORMAL `` guards |
| `constraints` | string | Optional path to a single `.sv` file with environment `assume property` statements (clock toggle, reset sequence, etc.). Read into the sby script *before* `properties:` so the assumes are in scope when asserts elaborate. Resolved relative to `fpv.yaml`. Analogous to `constraints:` in `pnr.yaml` — separates "environment" from "what to prove" and lets multiple verifications share one clock/reset boilerplate. |
| `mode` | string | One of `bmc`, `prove`, `cover`, `live`; defaults to `bmc` |
| `depth` | int | Cycle depth for the proof; defaults to 20 |
| `engines` | list | Sby engine specs (e.g. `smtbmc yices`, `abc pdr`); defaults to `["smtbmc yices"]` |
| `reglvl` | int or dict | Regression level; int for all tools, dict for per-tool with `default` |
| `tool_overrides` | dict | Optional per-tool overrides for `timeout` or `extra_args`, keyed by FPV tool name |
| `vacuity` | bool | Optional. When true (default for `bmc` / `prove`), run a secondary sby cover-mode pass over auto-derived covers for every `\|->` / `\|=>` antecedent in the property set. Default is false for `cover` / `live` modes. See [Vacuity covers](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/#vacuity-covers). |
| `coi` | bool | Optional. When true (default), run a yosys cone-of-influence pass after the primary proof and report the fraction of design cells reachable from at least one assertion. See [Cone-of-influence coverage](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/#cone-of-influence-coverage). |
| `frontend` | string | SystemVerilog frontend. `"verilog"` (default — yosys native, immediate + simple-concurrent SVA only) or `"slang"` (yosys-slang plugin — required for `\|->` / `\|=>` and SV `bind`). `slang` requires `cfg-fpv-tools[].opts.plugin-path` in root_config.yaml. See [Choosing a frontend](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/#choosing-a-frontend). |

**Runtime effects:**

- `rtl-buddy fpv` loads `fpv.yaml`, resolves the model's filelist via `models.yaml`, and dispatches to the backend selected by `tool`.
- The bundled `sby` backend generates a `.sby` config containing `[options]` (mode, depth, optional timeout), `[engines]`, `[script]` (Yosys read + prep), and `[files]` (resolved source paths), then invokes `sby -f -d <workdir> <config>`.
- Each verification writes the generated config, the full sby log, and the sby workdir under `artefacts/{name}/`; the workdir's `status` file is the authoritative pass/fail signal, with the process exit code as fallback.
- Counterexample VCDs (on FAIL) land at `artefacts/{name}/sby_workdir/engine_<N>/trace.vcd`.
- `rtl-buddy fpv <name> --list` lists configured verifications without running them.

---
