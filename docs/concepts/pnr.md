---
description: Run OpenROAD place-and-route from a mapped synthesis result, configure a physical platform, and inspect timing, DRC, GDS, and layout artefacts.
---

# Place-and-Route

`rb pnr` consumes a technology-mapped `rb synth` result, runs OpenROAD placement, clock-tree synthesis, and routing, then reports area, timing, and DRCs.

## Install the tools

OpenROAD 25Q1 or newer must be on `PATH` or configured in `cfg-pnr-tools`. RTL Buddy warns and continues with an older version, but that combination is not validated.

On macOS, use the project template's `tools/openroad/BUILD_OSX.md` source-build instructions.

KLayout is optional and used only for `--gds` and `--png`:

```bash
brew install --cask klayout
```

A missing KLayout skips GDS or PNG generation without failing the OpenROAD run.

## Define a P&R run

```yaml
rtl-buddy-filetype: pnr_config

runs:
  - name: demo_pnr_nangate45
    desc: Nangate45 typical-corner P&R
    tool: openroad
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan:
      utilization: 0.55
      aspect: 1.0
      core-margin: 2.0
    reglvl: 1000
```

Paths resolve from `pnr.yaml`. The named synthesis must already have produced `artefacts/<synth>/synth_netlist.v`. RTL Buddy takes the top module from that synthesis entry and takes Liberty and LEF assets from the selected physical platform.

Only `tool: openroad` is supported. Other tool names report `SKIP`. See [YAML Formats: pnr.yaml](../reference/yaml.md#pnryaml) for all fields.

## Configure the physical platform

PDK files are defined once under `cfg-pdks`. Select a process and corner for P&R under `cfg-pnr-platforms`:

```yaml
cfg-pnr-platforms:
  - name: nangate45_typ
    pdk: nangate45
    corner: typ
    cts-buffer: BUF_X4
    routing-layers:
      signal: metal2-metal8
      clock: metal4-metal8
```

The PDK entry supplies Liberty, technology and macro LEF, cell GDS, site, and other cell names. See [Synthesis: Configure tools and the PDK](synthesis.md#configure-tools-and-the-pdk) and the [root config schema](../reference/yaml.md#root_configyaml).

## Run P&R

```bash
rb pnr --list -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
```

`--png` implies `--gds`. RTL Buddy invokes KLayout after a successful OpenROAD run. KLayout conversion failures produce warnings but do not change the P&R verdict; use the OpenROAD timing and DRC results as the run outcome.

## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing; zero DRC lines indicate a clean route.

A run passes when OpenROAD exits 0 and emits no `[ERROR ...]` line. It skips when filtered by `reglvl` or when `tool:` is unsupported. Timing violations or DRC counts are reported as metrics; inspect the result policy for your project before using them as signoff gates.

## Inspect artefacts

Outputs land under `<pnr-dir>/artefacts/<run>/`.

| File | Purpose |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output and generated flow |
| `<top>.def` | Routed DEF |
| `<top>.routed.v` | Post-route gate-level netlist |
| `<top>.routed.sdc` | Post-route constraints |
| `<top>.routed.odb` | OpenROAD database used by post-P&R power |
| `timing.rpt` | Expanded worst-path timing |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `<top>.gds`, `<top>.png` | Optional KLayout outputs |
| `klayout.*.log` | Optional conversion logs |

Every file above except the logs is deleted before each run — including the optional KLayout outputs, which are cleared up front rather than at the streamout step, so a run that dies inside OpenROAD or on a host without KLayout leaves no older layout behind. A run that dies short of routing therefore leaves the outputs it never wrote absent rather than the previous run's. Unlike the other flows, this happens even when OpenROAD itself is missing — the clear is the first thing a run does — because `rb power` resolves `<top>.routed.odb` by path and must never be handed the previous run's database. For the same reason a run that reaches `write_db` and then dies — killed, exiting non-zero, or logging an `[ERROR ...]` line — has its outputs removed again, so a `FAIL` never leaves a routed database behind. The optional KLayout steps behave the same: a zero-length GDS or a half-rendered PNG is removed rather than left to be read as this run's layout. On failure, inspect `pnr.log`. If KLayout alone failed, inspect the corresponding `klayout.*.log` and rerun with `--gds` or `--png` after correcting the installation.
