---
description: How to run OpenROAD-driven gate-level power analysis with rtl_buddy via the rb power command, power.yaml, SAIF activity capture, and cfg-pnr-platforms.
---

# Power Analysis

> **Integration type:** Integrated tool. `rb power` is built around OpenROAD today; the backend interface (`BasePower` + `_POWER_BACKENDS` registry) is designed so commercial flows can be added without changing the schema.
>
> **External binary required:** `openroad` ≥ `25Q1` (same as `rb pnr`).
>
> See also: [Place-and-Route](pnr.md), [Synthesis](synthesis.md).

`rb power` runs OpenROAD's `report_power` on the tech-mapped netlist produced by an upstream `rb synth` run. It supports three activity sources — built-in static defaults, synthetic global toggle/duty, and per-signal activity from a SAIF or VCD trace — and reports total / internal / switching / leakage power.

## Where the numbers come from

The flow is **post-synth, gate-level**. It loads:

- The yosys tech-mapped netlist from the upstream synth run (`synth_netlist.v`).
- Liberty + tech LEF + macro LEF from the selected `cfg-pnr-platforms` entry.
- The SDC from `constraints:`.
- The activity model chosen in the run's `activity:` block.

LEF is loaded because OpenROAD's gate-level `read_verilog` requires a technology view in its in-memory DB; `report_power` itself only consults Liberty (per-cell internal/switching coefficients, leakage tables).

**Internal and leakage** numbers are gate-accurate (driven by Liberty). **Switching** is *under-estimated* — there is no real wire capacitance (no PnR + parasitic extraction) and no CTS-buffered clock tree, so the clock network looks like one flat net. For realistic post-route switching numbers, run after `rb pnr` and load SPEF — that flow is on the roadmap.

## Supported backend

Today only `openroad` is wired up. The `tool:` field in `power.yaml` selects it; the dispatch is a registry (`_POWER_BACKENDS` in `runner/power_runner.py`), so a commercial backend is one line plus a `BasePower` subclass under `tools/power_<name>.py`.

## Power config: `power.yaml`

```yaml
rtl-buddy-filetype: power_config

runs:
  - name: "demo_power_static"
    desc: "Static power on Nangate45 typ corner"
    tool: "openroad"
    mode: "static"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    reglvl: 1000

  - name: "demo_power_dynamic_synthetic"
    desc: "Dynamic power with synthetic global activity"
    tool: "openroad"
    mode: "dynamic"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    activity:
      default-toggle-rate: 0.2
      default-static-prob: 0.5
    reglvl: 1000

  - name: "demo_power_dynamic_saif"
    desc: "Dynamic power driven by simulation SAIF"
    tool: "openroad"
    mode: "dynamic"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    activity:
      saif: "../../verif/demo/artefacts/csr_smoke/dump.saif"
      scope: "tb_top/u_dut"
    reglvl: 1000
```

### Fields

| Field | Description |
|---|---|
| `name` | Run identifier; used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — only `openroad` is supported today |
| `mode` | `"static"` or `"dynamic"`. Static skips activity entirely; dynamic applies one of the activity sources below |
| `synth` | Name of the upstream `rb synth` entry to consume |
| `synth-path` | Path to the `synth.yaml` containing `synth`, resolved relative to `power.yaml` |
| `constraints` | SDC path (required), resolved relative to `power.yaml` |
| `platform` | `cfg-pnr-platforms` entry name — reused for Liberty + corner |
| `activity.saif` | Path to a SAIF v2 file (mutually exclusive with `vcd`) |
| `activity.vcd` | Path to a VCD trace (mutually exclusive with `saif`) |
| `activity.scope` | Hierarchical scope passed to OpenROAD's `-scope` flag when reading a trace. Only valid alongside `saif`/`vcd` — set without a trace, it raises a config-load error |
| `activity.default-toggle-rate` | Synthetic global activity rate (used when `mode: dynamic` and no trace is supplied). Default `0.1` |
| `activity.default-static-prob` | Synthetic global duty cycle. Default `0.5` |
| `reglvl` | Regression level for filtering (same semantics as `rb synth` / `rb pnr`) |

### Activity source resolution

Decision happens at config load (`PowerConfig.get_activity_source()`), not in the backend, so every backend agrees on the strategy:

| `mode` | `activity.saif` | `activity.vcd` | → resolved source |
|---|---|---|---|
| `static` | (ignored) | (ignored) | `default` — no activity command emitted |
| `dynamic` | set | — | `saif` — backend emits `read_saif` |
| `dynamic` | — | set | `vcd` — backend emits `read_power_activities -vcd` |
| `dynamic` | — | — | `synthetic` — backend emits `set_power_activity -global` |

The resolved source is recorded in the results table as the `Activity` column.

## Capturing a SAIF from simulation

`rb saif` converts an FST or VCD waveform trace into SAIF v2.0 (backward direction):

```bash
# 1. Run the sim in debug mode so it produces an FST
rb -M debug test csr_smoke

# 2. Convert the FST to SAIF
rb saif verif/<suite>/artefacts/csr_smoke/dump.fst verif/<suite>/artefacts/csr_smoke/dump.saif

# 3. Reference it from power.yaml (activity.saif: ...) and run rb power
rb power demo_power_dynamic_saif -c power/demo/power.yaml -l 1000
```

The converter walks the trace hierarchy, computes per-bit T0/T1/TX/TZ time-in-state and TC toggle counters, and emits SAIF in the trace's native timescale so values stay exact integers. Memory-array elements (FST `[N]` vars) are skipped — they don't correspond to gate-level nets in the synth netlist.

When the SAIF is rooted at the testbench (e.g. `tb_top`), pass `scope: "tb_top/u_dut"` so OpenROAD knows where in the SAIF tree the design top lives.

## Running power

```bash
# All runs in the default ./power.yaml
rb power

# A single run from a specific config
rb power demo_power_dynamic_saif -c power/demo/power.yaml

# Reglvl-gated runs
rb power -c power/demo/power.yaml -l 1000

# List runs without executing
rb power -c power/demo/power.yaml --list
```

### Regression

```bash
# Default: ./power_regression.yaml
rb power-regression

# Explicit config
rb power-regression -c power_regression.yaml -l 1000
```

`power_regression.yaml`:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs:
  - power/demo_block_a/power.yaml
  - power/demo_block_b/power.yaml
```

## Results table

```
                             Power Results Summary
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━┓
┃ Power  ┃ Result ┃ Desc   ┃ Mode  ┃Activity┃ Total ┃Internal┃ Switch┃Leakage ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━━┩
│ …static│ PASS   │ …      │static │default │ 422 µW│ 344 µW│22.6 µW│54.9 µW │
│ …synth │ PASS   │ …      │dynamic│synth…  │ 515 µW│ 363 µW│93.1 µW│59.3 µW │
│ …saif  │ PASS   │ …      │dynamic│saif    │ 906 µW│ 646 µW│ 203 µW│57.1 µW │
└────────┴────────┴────────┴───────┴────────┴───────┴───────┴───────┴────────┘
```

Power values auto-scale between W / mW / µW / nW for readability.

## Artefacts

Per-run outputs land under `power/<suite>/artefacts/<run>/`:

| File | Contents |
|---|---|
| `power.log` | Full OpenROAD log |
| `power.tcl` | Templated Tcl handed to OpenROAD |
| `power.rpt` | Raw `report_power` output (Total line is what the parser consumes) |

## Pass/fail detection

A run is PASS when:
1. `openroad` exits with code 0.
2. The log has no `[ERROR ...]` lines.
3. The `Total` line in `power.rpt` parses cleanly.

Otherwise FAIL is returned with the parser/exit-code message. SKIP is returned when the run's `reglvl` is above the `-l` filter or when `tool:` is not in the backend registry.

## Out of scope (today)

- **Post-PnR power.** Requires SPEF/parasitic input and a `netlist-source: pnr` selector. On the roadmap.
- **Per-instance power breakdown.** The current parser only takes the `Total` line; per-module/per-instance numbers are in `power.rpt` but not surfaced.
- **Multi-corner power signoff.** One corner per run; multi-corner needs a richer schema.
- **RTL-level power estimation** (Joules-style). Would need a different backend; the activity schema would extend without breaking.
