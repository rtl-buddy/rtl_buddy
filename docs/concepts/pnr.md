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

A missing KLayout skips GDS or PNG generation without failing the OpenROAD run. Install it later and [export the saved result](#export-a-saved-result) rather than rerunning P&R.

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

## Export a saved result

KLayout, a PDK's GDS or the layer properties often arrive after the P&R does. `rb pnr-export` streams an existing routed result out again — DEF → GDS → PNG — without rerunning synthesis or OpenROAD:

```bash
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds-mode strict
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --def ../saved/demo_top.def
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png-only --lyp dark.lyp --png-width 4096 --png-height 4096
```

Run selection, `-c`, `-l` and `--gds-mode` mean what they mean for `rb pnr`, and the export reads the same configuration: the technology, the PDK's `cell-gds`, the run's `gds-paths` and `lef-paths`, and the `gds-allow-empty` list. The design's name comes from the upstream `synth:` entry, read out of `synth.yaml` — the export needs the synthesis *configuration* and none of its artefacts. `rb tool-check --required-for pnr-export` therefore asks for KLayout and for nothing else.

Nothing is launched before the saved result has been checked. The routed DEF must exist, be non-empty, and declare the design this run's synth entry names — its own `DESIGN <top> ;` statement is compared, so a result left by another design fails with both names rather than being streamed into a layout named after a design it does not contain. A missing layer properties file, a PDK with no `klayout-tech`, a configured input that is not on disk, and a missing KLayout each stop the export the same way. Nothing here is decided from file timestamps: a checkout, a copy or an archive restore rewrites those in any order.

The export clears **only what an export publishes** — the GDS, the PNG, `def2stream.report.json`, `def2stream.inputs.json` and its own record. The routed DEF, ODB, netlist, SDC and every P&R report stay exactly as they were, on a failed export as much as on a successful one.

`--def <path>` exports a DEF from elsewhere, with the platform and the top still coming from the run, and needs a single named run. `--png-only` re-renders the PNG from the GDS already in the artefact directory: no stream-out, the GDS is an input and is never rewritten, and `--lyp`, `--png-width` and `--png-height` change how it is drawn. If a `def2stream.report.json` sits beside that GDS and says cells had no layout, the re-render carries the same qualifier; if no report sits beside it, nothing vouched for that layout, so the re-render reports it as qualified rather than complete.

### The export verdict

For `rb pnr` the export is a bonus over a P&R verdict, so in `preview` mode a failed export leaves the run passing. For `rb pnr-export` the export *is* the job:

- everything that was asked for, on disk — `PASS`, exit 0;
- a layout `preview` published with cells that have no GDS — `PASS` with the `GDS incomplete: …` qualifier, exit 0, exactly as in a `rb pnr` run;
- anything else, in either mode — `FAIL` with `fail_stage: export` and exit 1: no KLayout, no technology, an input off disk, a stream-out that wrote nothing, a `--png` render that failed, or, under `strict`, a layout with cells that have no GDS.

Rows carry the same fields a `rb pnr` row does — `gds_status`, `gds_mode`, `gds_missing_cells`, `gds_missing_cell_count`, `gds_allowed_empty_cells`, `gds_path`, `png_path` — plus `export_provenance`.

### The export record

Each export writes `export.provenance.json` into the artefact directory, whatever the outcome — a failure so early that the design cannot even be named writes none, and clears the previous one with the rest. It is the export's own document: it never edits `pnr.log`, `pnr.tcl`, the reports or the results of the P&R run it exports. A fresh `rb pnr` run clears it, because the run replaces the DEF the record describes.

```json
{
  "schema_version": 1,
  "generator": "rtl-buddy 6.53.0",
  "generated_at": "2026-09-21T15:04:12+08:00",
  "command": "pnr-export",
  "run": "demo_pnr_nangate45", "top": "demo_top",
  "gds_mode": "preview", "png_only": false,
  "tool": {"name": "klayout", "path": "/opt/homebrew/bin/klayout", "version": "KLayout 0.30.8"},
  "inputs": {
    "def": {"path": "…/demo_top.def", "size": 1415786, "sha256": "…"},
    "gds": null, "tech": "…/FreePDK45.lyt",
    "cell_gds": ["…"], "lef": ["…"], "missing": [], "allow_empty": ["fakeram45_*"]
  },
  "render": {"requested": true, "lyp": "…/FreePDK45.lyp", "width": 2048, "height": 2048},
  "outputs": {"gds": "…/demo_top.gds", "png": "…/demo_top.png"},
  "outcome": {"status": "complete", "delivered": true, "missing_cells": [], "allowed_empty_cells": [], "desc": ""}
}
```

Paths are project-relative POSIX where they can be, as in `phys-manifest.json`. Exactly one of `inputs.def` and `inputs.gds` is set — the DEF a stream-out read, or the GDS a re-render rendered — and it carries the size and the SHA-256 of the bytes that were read, which is what a later reader compares to decide whether the layout still belongs to the result beside it.

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
| `export.provenance.json` | What an `rb pnr-export` invocation read and produced |
| `klayout.*.log` | Optional conversion logs |

Every file above except the logs is deleted before each run — including the optional KLayout outputs, which are cleared up front rather than at the streamout step, so a run that dies inside OpenROAD or on a host without KLayout leaves no older layout behind. A run that dies short of routing therefore leaves the outputs it never wrote absent rather than the previous run's. Unlike the other flows, this happens even when OpenROAD itself is missing — the clear is the first thing a run does — because `rb power` resolves `<top>.routed.odb` by path and must never be handed the previous run's database. For the same reason a run that reaches `write_db` and then dies — killed, exiting non-zero, or logging an `[ERROR ...]` line — has its outputs removed again, so a `FAIL` never leaves a routed database behind. `pnr.tcl` and `def2stream.inputs.json` are cleared only in that first up-front pass, so a rerun that never reaches script generation does not leave the previous run's flow script or stream-out inputs looking like the ones it used — but a run that does reach the tools keeps them even when it fails, because they are what `pnr.log` and `klayout.def2stream.log` are logs of. The optional KLayout steps behave the same: a zero-length GDS, a half-rendered PNG, and the stream-out report that would otherwise say a layout is complete are removed rather than left to be read as this run's. A `strict` export failure removes the layout and its report but keeps the routed outputs, because P&R itself succeeded. `rb pnr-export` clears a narrower set still — the layout, the image, the stream-out report, the input manifest and its own record — and never the routed DEF, ODB, netlist or SDC it reads. On failure, inspect `pnr.log`. If KLayout alone failed, inspect the corresponding `klayout.*.log` and rerun with `--gds` or `--png` after correcting the installation.
