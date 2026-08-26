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

  - name: "openroad"
    tool: "openroad"     # executable name (must be on PATH)
    opts:
      strategy: "AREA"   # AREA (default) | TIMING | TIMING_ANNEAL | TIMING_GENETIC
```

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

### PDK library configuration

Liberty files for technology mapping are registered under `cfg-synth-libs`. Paths are resolved relative to `root_config.yaml`:

```yaml
cfg-synth-libs:
  - name: "sky130hd_tt"
    path: "pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
    lef-paths:                                          # required for OpenROAD backend
      - "pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef"
```

The `libraries` list in `synth.yaml` references entries by name.

- **Yosys backend:** uses `path` (liberty) for `read_liberty` → `dfflibmap` → `abc -liberty` → `write_verilog`. `lef-paths` is ignored.
- **OpenROAD backend:** requires both `path` (liberty) for timing and `lef-paths` (LEF) for technology loading. Without `lef-paths` the run fails immediately with an actionable error.

PDK files are typically large and should be gitignored. Provide a download script:

```bash
# pdk/download_pdk.sh
curl -fL <liberty-url> -o pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
curl -fL <lef-url>     -o pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef
```
