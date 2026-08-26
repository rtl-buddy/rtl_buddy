## Root config: `root_config.yaml`

### Synthesis tool configuration

Synthesis tool defaults live under `cfg-synth-tools`. Multiple tools can be listed; the `tool` field in `synth.yaml` selects which entry to use:

```yaml
cfg-synth-tools:
  - name: "yosys"
    tool: "yosys"        # executable name (must be on PATH)
    opts:
      synth-args: ""
      abc-args: ""
      frontend: "verilog"      # "verilog" (default) | "slang"
      plugin-path: ""          # required if frontend: slang

  - name: "openroad"
    tool: "openroad"     # executable name (must be on PATH)
    opts:
      strategy: "AREA"   # AREA (default) | TIMING | TIMING_ANNEAL | TIMING_GENETIC
      frontend: "verilog"
      plugin-path: ""
```

#### SystemVerilog frontend

`opts.frontend` chooses the parser Yosys uses to read the design:

| Value | Behaviour |
|-------|-----------|
| `"verilog"` (default) | `read_verilog -sv -defer` per source — lazy elaboration, fast, supports the SystemVerilog subset built into the rtl-buddy Yosys fork. |
| `"slang"` | Loads the [yosys-slang](https://github.com/povik/yosys-slang) plugin and calls `read_slang --top <top> --std 1800-2017` — full SV-2017 (`import pkg::*`, packed-struct typedefs, virtual interfaces, complex generates). Elaboration is eager, so `params:` are folded into `read_slang -GNAME=VAL` and `defines:` into `-DNAME=VAL` (subsequent `chparam` is skipped). |

When `frontend: slang`, `opts.plugin-path` must be set to the location of yosys-slang's `slang.so`. Absolute paths pass through unchanged; relative paths resolve against the project root (the directory containing `root_config.yaml`). Build instructions for the plugin are in [`yosys-slang's README`](https://github.com/povik/yosys-slang#building).

Per-block opt-in (leaves other blocks on the legacy frontend):

```yaml
# synth.yaml
- name: "<block>_synth"
  tool: "yosys"
  model: "<top>"
  model_path: "../../design/<block>/models.yaml"
  tool_overrides:
    yosys:
      frontend: "slang"
      plugin_path: "../yosys-slang/build/slang.so"
```

The OpenROAD backend inherits the same selection — it runs Yosys for elaboration before handing the netlist to OpenROAD for STA/placement. Even when the synth's `tool:` is `openroad`, the per-block override key is `tool_overrides.yosys` (the elaboration tool), not `tool_overrides.openroad`:

```yaml
# synth.yaml
- name: "<block>_or"
  tool: "openroad"          # full Yosys + OpenROAD STA flow
  tool_overrides:
    yosys:                  # elaboration-stage opts → live under yosys
      frontend: "slang"
      plugin_path: "../yosys-slang/build/slang.so"
```

> **Naming convention — `plugin-path` vs `plugin_path`:** under `cfg-synth-tools.opts` (above) the YAML field is **kebab-case** (`plugin-path`, `synth-args`, `abc-args`) — that's the schema's canonical form. Under `tool_overrides.yosys` keys are the **Python attribute names** (snake_case: `plugin_path`, `synth_args`), because the override dict is merged at the attribute level rather than re-deserialised through the YAML schema. Same field, two names, depending on where it lives.

#### Strategy

The `strategy` option controls optional OpenROAD resynthesis after timing analysis:

| `strategy` | Effect |
|------------|--------|
| `AREA` (default) | No resynthesis; report area and timing only |
| `TIMING` / `TIMING_ANNEAL` | Run `resynth_annealing` after loading the netlist |
| `TIMING_GENETIC` | Run `resynth_genetic` after loading the netlist |

### Synthesis effort configuration

`cfg-synth-efforts` defines named levels that shape both the Yosys stage and the OpenROAD stage. Reference an entry from `synth.yaml` `effort:` or `rb synth --effort <name>`.

```yaml
cfg-synth-efforts:
  - name: "quick"
    # Yosys-only fast path; skips OpenROAD entirely.
    # Returns gate count + area only. No LEF/STA needed.
    yosys:
      synth-args: "-flatten"
      abc-args: "-fast"
    openroad:
      run: false

  - name: "standard"
    # Default behaviour: Yosys + OpenROAD STA with ideal wires (zero RC).
    openroad:
      run: true

  - name: "accurate"
    # Apply the Liberty default_wire_load model for RC-aware pre-layout
    # timing without needing a tech LEF + floorplan. Swap pre-sta-tcl
    # for initialize_floorplan + global_placement + estimate_parasitics
    # once a tech LEF is available.
    openroad:
      run: true
      pre-sta-tcl: |
        set_wire_load_mode top
        set_wire_load_model -name Small
```

Effort schema:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique effort identifier; referenced from `synth.yaml` or `--effort` |
| `yosys.synth-args` | string | Appended to the `synth -top` command in both backends |
| `yosys.abc-args` | string | Used by the unmapped ABC step (Yosys backend without `libraries`) |
| `openroad.run` | bool | When `false`, a `tool: openroad` synthesis falls back to the Yosys-only backend (no LEF/STA required) — the recommended quick-look path |
| `openroad.pre-sta-tcl` | string | Raw Tcl snippet injected into `synth.tcl` between `read_sdc` and `report_checks` — use for floorplan/placement/parasitic-estimation before timing analysis |

> **Built-in fallback:** when `cfg-synth-efforts` is not configured (or the synthesis omits `effort:` and no override is passed), an internal `standard` effort with all defaults is used. Existing projects therefore need no migration.

> **Tradeoff:** `pre-sta-tcl` is a raw snippet — powerful, but errors in it surface only at OpenROAD runtime. Test new snippets against a small design before adopting them in a regression.

### Example: quick / standard / accurate on SKY130

Running the same DMA design at all three levels (ip_dma, sky130hd_tt, 5-clock SDC):

| Effort | Gates | WNS | TNS | Notes |
|--------|-------|-----|-----|-------|
| `quick` | 213 | +3.314 ns | — | Yosys-only with `-flatten` / `-fast`; aggressive optimisation; no STA |
| `standard` | 10218 | −1.172 ns | −7835.2 ns | OpenROAD STA with ideal wires (zero RC) |
| `accurate` | 10218 | −1.347 ns | −8418.4 ns | OpenROAD STA + Liberty wire-load model |

The pessimization between `standard` and `accurate` (−1.347 vs −1.172 ns WNS) shows the wire-load model adding parasitic RC; the gate count is unchanged because the Yosys stage runs identically.

### PDK and synth platform configuration

PDK assets live under `cfg-pdks` — one entry per process, with corners as sub-fields. Each entry owns *everything* PDK-bound (Liberty per corner, tech-LEF, macro-LEF, cell-GDS, KLayout `.lyt`/`.lyp`, SITE, tie/fill cells); synth and P&R consume what they need.

`cfg-synth-platforms` is a thin selector layer: each entry references a PDK + corner. `synth.yaml` then picks a platform name via `platform:`. All paths are resolved relative to `root_config.yaml`.

```yaml
cfg-pdks:
  - name: "sky130hd"
    site: "unithd"
    corners:
      tt: "pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
    tech-lef:  "pdk/sky130hd/lef/sky130_fd_sc_hd.tlef"
    macro-lef: "pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef"

cfg-synth-platforms:
  - name: "sky130hd_tt"
    pdk: "sky130hd"
    corner: "tt"
```

- **Yosys backend:** uses the platform's Liberty for `read_liberty` → `dfflibmap` → `abc -liberty` → `write_verilog`. LEF is ignored.
- **OpenROAD backend:** requires both Liberty and LEF. The `tech-lef` and `macro-lef` on the PDK are passed through automatically; per-block extras can be added via `lef-paths:` on the `synth.yaml` entry. A platform with no LEF assets fails immediately with an actionable error.

PDK files are typically large and should be gitignored. Provide a download script:

```bash
# pdk/download_pdk.sh
curl -fL <liberty-url> -o pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
curl -fL <lef-url>     -o pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef
```
