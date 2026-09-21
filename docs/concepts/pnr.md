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
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds-mode strict
```

`--png` and `--gds-mode` imply `--gds`. RTL Buddy invokes KLayout after a successful OpenROAD run. In the default `preview` mode a KLayout failure produces a warning and does not change the P&R verdict; use the OpenROAD timing and DRC results as the run outcome. In `strict` mode an export that could not be delivered fails the run — see [Stream-out completeness](#stream-out-completeness).

### Stream-out inputs

Stream-out reads more than the routed DEF. The layout comes from the PDK's `cell-gds` — one path or a list of them — followed by the run's own `gds-paths`, which is where the layout of a hard macro belongs: an OpenRAM SRAM has its LEF in `lef-paths` and its GDS in `gds-paths`, and each path resolves against the file that names it, `root_config.yaml` for the PDK and `pnr.yaml` for the run. The DEF reader is also given the LEFs, so it can resolve the masters the DEF instantiates: technology LEF, the PDK's macro LEF, then the run's `lef-paths`, in that order and de-duplicated, appended to whatever the KLayout technology file already lists rather than replacing it. Both lists reach KLayout through `def2stream.inputs.json` in the artefact directory, which is also where to read back what a given run streamed.

An input the config names and the disk does not have stops the export before KLayout is launched, with every missing path reported at once — a stream-out run without it succeeds and writes a GDS with the unresolvable cells left empty, which is a layout that looks produced.

### Stream-out completeness

A cell the DEF instantiates whose layout is in none of the GDS files is streamed as an empty placeholder. Sometimes that is deliberate — an ORFS `fakeram45` macro exists only in LEF, and a preview of the floorplan around it is exactly what was wanted — and sometimes it is a design that forgot its real SRAM GDS and got a plausible picture with a hole in it. `gds-mode` says which the run means:

```yaml
runs:
  - name: demo_pnr_signoff
    # ...
    gds-mode: strict          # default: preview
    gds-allow-empty:          # cells that are empty on purpose
      - fakeram45_*
```

- `preview` (the default) keeps the layout and reports what is missing from it: `pnr.gds_incomplete` at WARNING naming every cell, `gds_status: incomplete` with `gds_missing_cells` and `gds_missing_cell_count` in `--machine` output, `GDS incomplete: …` in the run's description, and `gds+png (incomplete: N missing)` in the summary's Outputs column. The run still passes; the preview is never reported as a complete stream-out.
- `strict` refuses to publish it. Cells with no layout, a missing KLayout executable, a PDK with no `klayout-tech`, a configured input that is not on disk, a stream-out that failed for any other reason, and a `--png` render that failed each make the export a `FAIL` whose `fail_stage` is `export`. The GDS, the PNG and the stream-out report are removed, so nothing is left to be read as a successful artefact. The routed DEF, netlist, SDC and ODB stay: OpenROAD finished cleanly and those outputs are its, not the export's. Because the failure is not the flow's verdict on the design, an `xfail:` marker does not excuse it.

`gds-allow-empty` takes cell names or `fnmatch` globs, matched case-sensitively, and is the per-run form of the legacy `GDS_ALLOW_EMPTY` environment regex, which is still honoured. A cell it covers is not missing in either mode; it is reported as intentionally empty, in `gds_allowed_empty_cells` and as `(N empty by design)` in the Outputs column.

Completeness is decided from `def2stream.report.json`, which the bundled KLayout helper writes into the artefact directory after the layout — not from KLayout's console output. A run whose report is absent, unreadable or from another schema has a failed export, whatever else is on disk: nothing vouched for that layout.

## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing; zero DRC lines indicate a clean route.

A run passes when OpenROAD exits 0 and emits no `[ERROR ...]` line, and — with `gds-mode: strict` — when the requested export was delivered complete. It skips when filtered by `reglvl` or when `tool:` is unsupported. Timing violations or DRC counts are reported as metrics; inspect the result policy for your project before using them as signoff gates.

## Inspect artefacts

Outputs land under `<pnr-dir>/artefacts/<run>/`.

| File | Purpose |
| --- | --- |
| `pnr.log`, `pnr.tcl` | OpenROAD output and generated flow |
| `def2stream.inputs.json` | GDS, LEF and allow-empty list the optional KLayout stream-out read |
| `def2stream.report.json` | Which cells the stream-out could not fill, and whether it was complete |
| `<top>.def` | Routed DEF |
| `<top>.routed.v` | Post-route gate-level netlist |
| `<top>.routed.sdc` | Post-route constraints |
| `<top>.routed.odb` | OpenROAD database used by post-P&R power |
| `timing.rpt` | Expanded worst-path timing |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `<top>.gds`, `<top>.png` | Optional KLayout outputs |
| `klayout.*.log` | Optional conversion logs |

Every file above except the logs is deleted before each run — including the optional KLayout outputs, which are cleared up front rather than at the streamout step, so a run that dies inside OpenROAD or on a host without KLayout leaves no older layout behind. A run that dies short of routing therefore leaves the outputs it never wrote absent rather than the previous run's. Unlike the other flows, this happens even when OpenROAD itself is missing — the clear is the first thing a run does — because `rb power` resolves `<top>.routed.odb` by path and must never be handed the previous run's database. For the same reason a run that reaches `write_db` and then dies — killed, exiting non-zero, or logging an `[ERROR ...]` line — has its outputs removed again, so a `FAIL` never leaves a routed database behind. `pnr.tcl` and `def2stream.inputs.json` are cleared only in that first up-front pass, so a rerun that never reaches script generation does not leave the previous run's flow script or stream-out inputs looking like the ones it used — but a run that does reach the tools keeps them even when it fails, because they are what `pnr.log` and `klayout.def2stream.log` are logs of. The optional KLayout steps behave the same: a zero-length GDS, a half-rendered PNG, and the stream-out report that would otherwise say a layout is complete are removed rather than left to be read as this run's. A `strict` export failure removes the layout and its report but keeps the routed outputs, because P&R itself succeeded. On failure, inspect `pnr.log`. If KLayout alone failed, inspect the corresponding `klayout.*.log` and rerun with `--gds` or `--png` after correcting the installation.
