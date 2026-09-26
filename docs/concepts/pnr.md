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

## Tune the process-dependent steps

The flow's placement, clock-tree and power-grid steps are calibrated for Nangate45. A different process declares its own values; every key below is optional, and a config that sets none of them gets the behaviour the flow had before they existed.

```yaml
cfg-pdks:
  - name: sky130hd
    # ...
    placement:
      density: 0.55        # default 0.7
      padding: 2           # default 1, in sites
      macro-halo: 30.0     # default 20.0, in microns
    dont-use-cells:
      - "sky130_fd_sc_hd__probe*"
    pdn-config: pdk/sky130hd/pdn.tcl

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    placement:
      density: 0.6         # this platform only; padding stays the PDK's 2
    dont-use-cells:        # added to the PDK's list, never replacing it
      - "sky130_fd_sc_hd__lpflow_*"
```

**Placement.** `density` is the `global_placement` target utilization (`> 0` and `<= 1`); `padding` is the cell padding in sites, applied to both `-pad_left` and `-pad_right`. Both belong on the PDK, where they describe the cell architecture, and a P&R platform may override either of them for one selection — field by field, so a platform that names only `density` keeps the PDK's `padding`. Padding applies to global placement; detailed placement runs unpadded, as it always has.

**Macro halo.** `macro-halo` is the minimum channel, in microns, the [macro packer](#macro-placement) keeps between two macros and between a macro and each core edge. The default is 20.0 µm, which is what `pdngen` needs to repair a channel rather than fail it: a repair has to fit two straps and the spacing between them inside the halo the PDN's own macro grid reserves, and on sky130hd — met4/met5 straps 1.6 µm wide, a default spacing of half the 27.14 µm pitch less the width, and 2 µm of macro-grid halo per side — that is about 19.2 µm. A 12 µm channel measurably is not enough: it leaves `[ERROR PDN-0179] Unable to repair all channels`. Nangate45 ships no `pdn-config`, so there the halo costs placement area only, about fourteen standard-cell rows. Raise it for a process with a coarser grid, or when a macro needs more routing room; lower it, at the cost of PDN repairability, when macros will not otherwise fit.

**Don't-use cells.** `dont-use-cells` is one list of cell names or `*`-patterns, written on the PDK and read by both flows: P&R emits `set_dont_use` before the floorplan, so no placement, repair or CTS pass can pick one, and synthesis passes the same patterns to Yosys (`dfflibmap -dont_use`, `abc -dont_use`) and, on the OpenROAD backend, to the resynthesis stage's own `set_dont_use`. Write one pattern per list entry; an entry containing whitespace is rejected. Matching is each tool's own: OpenROAD and `dfflibmap` take simple globs, `abc` forwards each pattern to ABC's library exclusion, so confirm the routed netlist is free of the cells you meant to exclude.

A synth or P&R platform may name its own `dont-use-cells` for one selection; a platform's list reaches only its own flow, the PDK's reaches both. Patterns support only the `*` and `?` wildcards; Tcl metacharacters (`[ ] { } $ " \ ;`) are rejected when the config loads. They are *added* to the PDK's list — PDK entries first, a pattern named by both kept once — so a platform can exclude more than its PDK but never less, and a platform that adds nothing renders the Tcl the PDK alone did. With any cell excluded, the flow also checks the routed design itself, after hold repair and detailed routing and before fill insertion or any output is written: every placed instance whose master matches a pattern (by the same `get_lib_cells` matching `set_dont_use` used) is printed as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>` in `pnr.log`, and the run fails naming the first of them. `set_dont_use` only stops the resizer and CTS from *choosing* a cell, so this is what catches one the synthesis netlist already instantiates, and a don't-use cell is never reported as a plain PASS (#656); like any other verdict on the design, an `xfail:` marker on the run can still excuse it. A pattern that matches no Liberty cell excludes nothing — OpenROAD reports `[WARNING STA-0122] cell '<pattern>' not found.` (or `STA-0121` for the library half of a `lib/cell` pattern) and carries on — so rb logs a `pnr.dont_use_unmatched` warning naming it; check it for typos. With no cell excluded, neither the `set_dont_use` nor the check is emitted.

**Power grid.** `pdn-config` is a path — resolved from `root_config.yaml`, like the PDK's other paths — to a Tcl snippet that *declares* the grid: `add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`. The flow sources it after macro placement and calls `pdngen` itself, the same split ORFS uses for `PDN_TCL`, so the snippet must not call `pdngen`. Leave the key unset and no PDN Tcl is emitted at all. A snippet the config names and the disk does not have fails the run at `setup`, before OpenROAD is launched.

**CTS buffers.** `cts-buffer` still takes a single name. Given a list, every entry becomes the CTS `-buf_list` and the first entry becomes `-root_buf`, so order the list with the root buffer first. `cts-buffer: [BUF_X4]` and `cts-buffer: BUF_X4` are the same configuration.

### Macro placement

A design whose netlist instantiates hard macros — an SRAM, or a partition hardened by an earlier P&R run — gets an automatic macro placement before the power grid is built. Every block instance is packed by its *own* footprint: macros are sorted tallest first (then widest, then by name, so the result never depends on the order the database returns instances) and packed left to right into rows whose height is the tallest macro in the row, keeping `placement.macro-halo` between neighbours and at every core edge. Each origin is snapped up to the standard-cell site grid counted from the core corner (the site width in x, the row height in y) — up, so snapping can only widen a channel — and the instance is left `FIRM`, which global placement and the detailed placer then respect. Row alignment is what keeps the detailed placer's padding check (`DPL-0011`) honest: it rounds a macro to its nearest row, so a bottom edge between two rows can make it scan the row of standard cells above the macro as the macro's own.

Rows start at the core's bottom-left corner, so macro locations differ from the grid placement this replaced: a single macro sits in the corner, a halo from both edges, where it used to be centred in the core. Macros still place legally in every case that placed before, and the free area they leave is contiguous instead of split around a centred block.

Packing by footprint is what lets a mixed-size set fit a floorplan sized for the design. Three macros of 479.78 × 397.50, 98.94 × 98.94 and 89.47 × 89.47 µm need a 695 × 695 µm core this way; sized into equal slots, as the flow did before, the smallest core that held them was 959.56 × 795.00 µm (#626).

When the macros do not fit, the error says what was tried — the halo, the largest footprint, the macro area against the core area — and the smallest core at the run's aspect ratio that this packer would fit, so the `floorplan` block can be corrected in one step:

```
3 macros do not fit the 500.000 x 500.000 um floorplan core
  tried: shelf packing, tallest macro first, keeping a 20.000 um halo (placement.macro-halo) at every core edge and between macros
  macros: largest footprint 479.780 x 397.500 um, 208506.6 um2 of macro area in a 250000.0 um2 core
  smallest core at this aspect ratio that would fit: 556.440 x 556.440 um
  lower the floorplan utilization, change its aspect ratio, or lower placement.macro-halo
```

A design with no macros is unaffected: the packer's procedures are defined in `pnr.tcl` and never called.

## Constrain boundary pins

Set `pin-constraints: pins.tcl` in a run to assign pins to boundary edges or
groups. The path is relative to `pnr.yaml`; it is sourced after floorplan and
track initialization, immediately before `place_pins`. Missing files fail.
Without this key the default unconstrained placement is unchanged.

For example, `pins.tcl` can contain:

```tcl
set_io_pin_constraint -pin_names {req_* clk} -region left:*
set_io_pin_constraint -pin_names {rsp_*} -region right:*
```

Do not put these commands in SDC: SDC is read before the floorplan exists,
when whole-edge intervals can resolve to zero length. Keep clock pin layers
compatible with the platform's clock-routing range.

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

### OpenROAD threads

OpenROAD runs on one thread unless the run asks for more, and reserving CPUs from a scheduler does not change that on its own. Set `threads:` on the run:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: 8        # or: auto
```

| Value | Threads OpenROAD is given |
| --- | --- |
| unset | 1, OpenROAD's default. `pnr.tcl` is unchanged and carries no `set_thread_count` |
| a positive integer | That many, emitted as `set_thread_count N` before the first `read_liberty` |
| `auto` | The CPUs of the allocation the run is in; 1 when there is none. Never the host's core count |

Zero, negative numbers, booleans, quoted numbers and any other string fail configuration loading. `0` is refused rather than passed through because OpenROAD reads it as "every core on the host".

**Allocations.** `rb pnr` itself is not dispatched; run it inside an allocation (`srun -c 8 rb pnr ...`, or from an `sbatch` script) to give OpenROAD CPUs. RTL Buddy treats as the allocation, taking the smaller when both apply:

- inside a Slurm job (`SLURM_JOB_ID` set), `SLURM_CPUS_PER_TASK`, else `SLURM_CPUS_ON_NODE`;
- a CPU affinity mask smaller than the machine (`taskset`, a cpuset cgroup, a bound Slurm step; Linux only).

A count above the allocation is clamped to it, and the run logs `openroad.threads_capped` at WARNING naming both numbers; it never runs more threads than were reserved. The clamp is a warning rather than an error so one checked-in `pnr.yaml` works in allocations of any size. Outside an allocation an explicit count is used as given. OpenROAD then applies its own limit, the host's hardware thread count. A container CPU quota (`docker --cpus`) is not visible to either check. Simulator compile and run concurrency are unaffected.

**Provenance.** The resolved plan is logged as `openroad.threads`. The run's result carries `openroad_threads`: the `requested` value, the `effective` count, and the `allocation` and its `allocation_source`. `effective` is the count OpenROAD itself reported (`[INFO ORD-0030] Using N thread(s).` in `pnr.log`) when it reported one, otherwise the count RTL Buddy set, which is 1 when the key is unset. The same key and contract apply to [`rb power`](power.md) and to the OpenROAD stage of [`rb synth`](synthesis.md). The thread count is not part of any result fingerprint. OpenROAD documents its multi-threaded stages as producing the same result, but no bit-for-bit match across thread counts is claimed here; compare DRC and slack yourself if it matters.

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
