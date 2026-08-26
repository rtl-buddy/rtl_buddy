---
description: How to run synthesis flows with rtl_buddy using synth.yaml, cfg-synth-tools, cfg-pdks + cfg-synth-platforms, and the rb synth command.
---

# Synthesis

> **Integration type:** Pluggable. `rb synth` selects a synthesis tool via `tool:` in `synth.yaml`; the schema is open for future backends.
>
> **Curated tools (`tool:` values):** `yosys` (Yosys-only flow); `openroad` (Yosys + OpenROAD-STA two-stage flow).
>
> **External binaries required:** `yosys` (the [rtl-buddy/yosys fork](https://github.com/rtl-buddy/yosys), see [Installing Yosys](#installing-yosys)); plus `openroad` on `PATH` when `tool: openroad` is selected — see [Installing OpenROAD](#installing-openroad).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rtl_buddy` provides a tool-agnostic synthesis flow that mirrors the simulation workflow. Synthesis runs are described in `synth.yaml` files; tool-specific defaults and PDK library paths live in `root_config.yaml`.

## Supported backends

`rb synth` ships two backends selectable via `tool:` in `synth.yaml`. Both backends use Yosys to map RTL to a gate-level netlist; they differ in whether OpenROAD is run afterwards for static timing analysis.

| `tool:` | Backend | Multi-clock SDC | Reports |
|---------|---------|-----------------|---------|
| `yosys` | Yosys + ABC (single stage) | Workaround (min period) | Gates, Area, WNS |
| `openroad` | Yosys (stage 1) + OpenROAD STA (stage 2) | Native `read_sdc` | Gates, Area, WNS, TNS |

The `openroad` backend removes the multi-clock SDC workaround: stage 1 maps RTL to a gate-level netlist with Yosys, stage 2 feeds that netlist into OpenROAD which loads the SDC natively and reports WNS (actual worst slack from `report_checks`) and TNS (total negative slack).

## Installing Yosys

`rtl_buddy` uses the [rtl-buddy fork of Yosys](https://github.com/rtl-buddy/yosys), which tracks upstream with rtl-buddy-specific patches. Build from source:

```bash
# Install deps with brew. Adjust to your package manager as needed.
brew install cmake python tcl-tk libffi readline

# Clone, build and install
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang   # or `make config-gcc` on Linux
make -j 8           # adjust to no. of CPU cores if needed
make install        # installs to /usr/local/bin/yosys
```

Verify the install:

```bash
yosys --version
```

The `yosys` binary must be on `PATH` when `rb synth` is invoked.

### Optional: yosys-slang plugin

For designs that use SystemVerilog-2017 features Yosys's built-in frontend doesn't accept (e.g. `import pkg::*`, packed-struct typedefs, complex package generates), build the [yosys-slang](https://github.com/povik/yosys-slang) plugin against the same Yosys you just installed:

```bash
git clone --recursive https://github.com/povik/yosys-slang.git
cd yosys-slang
make -j 8           # produces build/slang.so
make install        # optional: copies into $(yosys-config --datdir)/plugins/
```

Wire it into `rb synth` by setting `opts.frontend: "slang"` and `opts.plugin-path` under `cfg-synth-tools` (see [`SystemVerilog frontend`](#systemverilog-frontend) below). Skip this step entirely if your designs work with the default `frontend: "verilog"`.

## Installing OpenROAD

OpenROAD is required only for the `openroad` backend. It must be built from source on macOS — no official binaries are published. See the build notes in your project's `tools/openroad/SETUP_OSX.md` (the starter template uses that filename) for the full procedure. After building, symlink the binary to a directory on `PATH`:

```bash
ln -s /path/to/OpenROAD/build/bin/openroad /usr/local/bin/openroad
openroad -version
```

The `openroad` binary must be on `PATH` when `rb synth` is invoked with `tool: "openroad"`.

## Synthesis config: `synth.yaml`

A `synth.yaml` file defines one or more synthesis runs for a block.

```yaml
rtl-buddy-filetype: synth_config

syntheses:
  # Technology-independent run
  - name: "sandbox_synth"
    desc: "Synthesize sandbox module with Yosys"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "yosys"
    reglvl: 0

  # Technology-mapped run targeting SKY130 (Yosys backend)
  - name: "sandbox_sky130"
    desc: "Synthesize sandbox module targeting SKY130 HD TT corner"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "yosys"
    platform: "sky130hd_tt"
    constraints: "constraints.sdc"
    params:
      WIDTH: 8
    defines:
      TARGET_SYNTH: 1
    reglvl: 0
    tool_overrides:
      yosys:
        synth_args: "-flatten"

  # Technology-mapped run with OpenROAD backend (native multi-clock SDC, WNS + TNS)
  - name: "sandbox_openroad"
    desc: "Synthesize sandbox module with OpenROAD timing analysis"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "openroad"
    platform: "sky130hd_tt"
    constraints: "constraints.sdc"
    reglvl: 0
```

### Synthesis fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in artefact paths |
| `desc` | Human-readable description |
| `model` | Model name from `models.yaml`; also used as the synthesis top module |
| `model_path` | Path to `models.yaml`, resolved relative to the `synth.yaml` directory |
| `tool` | Synthesis tool name — must match a `cfg-synth-tools` entry in `root_config.yaml` |
| `platform` | Optional synth platform name from `cfg-synth-platforms` (which in turn references a `cfg-pdks` entry); enables technology mapping |
| `constraints` | Optional SDC constraints file, resolved relative to `synth.yaml` |
| `params` | Optional key-value pairs passed as top-level parameter overrides (`chparam` in Yosys) |
| `defines` | Optional compile-time Verilog defines passed via `-D KEY=VALUE` |
| `reglvl` | Regression level (int or per-tool dict); same semantics as simulation `reglvl` |
| `tool_overrides` | Optional per-tool option overrides — keyed by tool name, merges over `cfg-synth-tools` defaults |

### SDC constraints

When `constraints` points to an SDC file, the Yosys backend extracts the `create_clock` period and passes it to ABC as `-D <period_ps>` for timing-driven technology mapping. The critical path delay is then used to compute WNS in the results table.

```sdc
create_clock -period 10.0 [get_ports clk]
set_input_delay  2.0 -clock clk [all_inputs]
set_output_delay 2.0 -clock clk [all_outputs]
```

**Multi-clock designs (Yosys backend):** ABC's `-D` flag takes a single timing window. When multiple `create_clock` entries are present, `rtl_buddy` uses the minimum period as a workaround and emits a warning. For correct per-domain timing analysis across multiple clocks, use the `openroad` backend, which passes the full SDC to `read_sdc` and handles each clock domain natively.

### Regression levels

`reglvl` works the same way as for simulation tests. Use `--reg-level` on `synth-regression` to filter by level.

```yaml
# Same level for all tools
reglvl: 0

# Tool-specific with fallback
reglvl:
  default: 0
  dc: 1000
```

### Per-tool overrides

`tool_overrides` is an escape hatch for tool-specific options that don't have a tool-agnostic equivalent. Keys match the `opts` fields in `cfg-synth-tools`:

```yaml
tool_overrides:
  yosys:
    synth_args: "-flatten -nordff"
    abc_args: "-fast"
```

### Effort levels

A synthesis can select a named **effort level** that controls how much work the flow does. Efforts are defined once in `root_config.yaml` under `cfg-synth-efforts` (see [below](#synthesis-effort-configuration)) and referenced per synthesis:

```yaml
syntheses:
  - name: "sandbox_quick"
    desc: "Fast iteration build — Yosys-only, no STA"
    model: "test_module"
    model_path: "../../design/sandbox/models.yaml"
    tool: "openroad"
    platform: "sky130hd_tt"
    constraints: "constraints.sdc"
    effort: "quick"      # references a cfg-synth-efforts entry
    reglvl: 0
```

When `effort:` is omitted, a built-in `standard` effort with all defaults is used — equivalent to the pre-effort behaviour. The `--effort <name>` CLI flag overrides the per-synthesis setting at runtime:

```bash
rb synth sandbox_openroad --effort quick     # force the quick path
rb synth-regression --effort accurate        # apply across a whole regression
```

Precedence for the same knob: per-synthesis `tool_overrides` > `cfg-synth-efforts` > `cfg-synth-tools`.

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

## Synthesis regression: `synth_regression.yaml`

`synth_regression.yaml` lists the `synth.yaml` files to include in a synthesis regression:

```yaml
rtl-buddy-filetype: synth_reg_config

synth-configs:
  - "synth/sandbox/synth.yaml"
  - "synth/dma/synth.yaml"
```

Paths are resolved relative to `synth_regression.yaml`.

## Running synthesis

Run all syntheses in a config:
```bash
rtl-buddy synth -c synth/sandbox/synth.yaml
```

Run a named synthesis:
```bash
rtl-buddy synth sandbox_sky130 -c synth/sandbox/synth.yaml
```

List syntheses without running:
```bash
rtl-buddy synth --list -c synth/sandbox/synth.yaml
```

Run a synthesis regression:
```bash
rtl-buddy synth-regression -c synth_regression.yaml
```

Run only up to regression level 0:
```bash
rtl-buddy synth-regression -c synth_regression.yaml --reg-level 0
```

## Results table

`rb synth` prints a results table after each run. Columns appear conditionally:

```
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Synthesis        ┃ Result ┃ Description      ┃ Gates ┃ Area       ┃ WNS       ┃ TNS       ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ sandbox_synth    │ PASS   │ Synthesis passed │ 18    │ -          │ -         │ -         │
│ sandbox_sky130   │ PASS   │ Synthesis passed │ 18    │ 178.92 µm² │ +8.882 ns │ -         │
│ sandbox_openroad │ PASS   │ Synthesis passed │ 18    │ 179.00 µm² │ +6.754 ns │ +0.000 ns │
└──────────────────┴────────┴──────────────────┴───────┴────────────┴───────────┴───────────┘
```

| Column | Source | When shown |
|--------|--------|-----------|
| **Gates** | Yosys `stat` cell count | All lib-mapped flows |
| **Area** | Yosys `stat -liberty` / OpenROAD `report_design_area` | Lib-mapped flows |
| **WNS** | Yosys: clock period − critical path delay; OpenROAD: `report_checks -path_delay max` | Lib-mapped flows with SDC |
| **TNS** | OpenROAD `report_tns` — sum of all negative endpoint slacks | OpenROAD backend with SDC |

WNS and TNS are positive when timing is met and negative when violated. TNS = 0 means no violations; a negative TNS indicates the total repair budget needed.

**WNS difference between backends:** Yosys computes WNS as `period − critical_path`, which always reports positive slack. OpenROAD's `report_checks` reports the actual worst slack across all timing paths. The two values are closely aligned for single-clock designs.

## Artefacts

Synthesis artefacts land under `artefacts/<synth_name>/` relative to the `synth.yaml` directory.

**Yosys backend:**

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist (resolved from `models.yaml`) |
| `synth.ys` | Generated Yosys script |
| `synth.log` | Captured Yosys stdout and stderr |
| `synth.rtlil` | Output netlist, technology-independent flow (RTLIL format) |
| `synth_netlist.v` | Output netlist, technology-mapped flow (Verilog) |

**OpenROAD backend:**

| File | Contents |
|------|----------|
| `synth.f` | Generated source filelist |
| `synth.ys` | Yosys script (stage 1 — maps RTL to gate-level netlist) |
| `synth_yosys.log` | Yosys stdout and stderr |
| `synth_netlist.v` | Gate-level Verilog produced by Yosys, fed into OpenROAD |
| `synth.tcl` | OpenROAD Tcl script (stage 2 — timing analysis) |
| `synth.log` | OpenROAD stdout and stderr |

## Pass/fail detection

**Yosys backend:** a run passes when the tool exits with code 0 and `synth.log` contains no lines starting with `ERROR:`.

**OpenROAD backend:** both stages must succeed. The Yosys stage applies the same exit-code and `ERROR:` check; the OpenROAD stage checks exit code and the absence of `[ERROR ...]` lines in `synth.log`.

Any other outcome is **FAIL** with a description in the results table.

## Full schema

See [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml) for the complete field reference.
