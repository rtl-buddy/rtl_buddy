---
description: Run OpenROAD place-and-route from a mapped synthesis result, configure a physical platform, and inspect timing, DRC, GDS, and layout artefacts.
---

# Place-and-Route

`rb pnr` takes a technology-mapped `rb synth` result, runs OpenROAD placement, clock-tree synthesis and routing, and reports area, timing and DRCs. It can also stream the layout out to GDS and PNG with KLayout, harden a block for reuse in a larger design, and keep stage checkpoints.

## Install the tools

OpenROAD 25Q1 or newer must be on `PATH` or configured in `cfg-pnr-tools`. An older version produces a warning and the run continues, but that combination is not validated. On macOS, build from source with the project template's `tools/openroad/BUILD_OSX.md`.

KLayout is optional and used only for `--gds` and `--png`:

```bash
brew install --cask klayout
```

Without KLayout, GDS and PNG generation is skipped and the OpenROAD run is not failed. Install KLayout later and [export the saved result](#export-a-saved-result) instead of rerunning P&R.

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

Paths resolve from `pnr.yaml`. The named synthesis must already have produced `artefacts/<synth>/synth_netlist.v`. The top module comes from that synthesis entry, and Liberty and LEF assets come from the selected physical platform.

Only `tool: openroad` is supported; any other value reports `SKIP`. See [YAML Formats: pnr.yaml](../reference/yaml.md#pnryaml) for all fields.

## Configure the physical platform

Define PDK files once under `cfg-pdks`. Select a process and corner for P&R under `cfg-pnr-platforms`:

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

The PDK entry supplies Liberty, technology and macro LEF, cell GDS, site and other cell names. See [Synthesis: Configure tools and the PDK](synthesis.md#configure-tools-and-the-pdk) and the [root config schema](../reference/yaml.md#root_configyaml).

## Sign off at several corners

`corners:` in place of `corner:` analyses a list of the PDK's corners together. `rb pnr` and `rb power` both read it. `rb synth` stays single-corner and uses its own `cfg-synth-platforms` entry.

```yaml
cfg-pdks:
  - name: sky130hd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
      ss: pdk/sky130hd/lib/sky130_fd_sc_hd__ss_n40C_1v40.lib
      ff: pdk/sky130hd/lib/sky130_fd_sc_hd__ff_n40C_1v95.lib

cfg-pnr-platforms:
  - name: sky130hd_mc
    pdk: sky130hd
    corners: [tt, ss, ff]    # the first entry is the primary corner
```

- All corners run in one OpenROAD session. `repair_design`, `repair_timing` and hold repair see every corner, and the final worst-slack and TNS reports give the worst across them.
- Clock-tree synthesis characterises buffers and wires at the first listed corner, which is why it is called the primary.
- A macro with a single Liberty in `lib-paths` is read into every corner, and OpenSTA logs `STA-1140 library ... already exists` once per extra corner. The warning is expected.
- That macro keeps the timing of the corner it was characterised at, so a corner can fail for reasons that belong to the macro. A TT SRAM Liberty with `max_transition: 0.04` stops `repair_design` at `RSZ-0090` at a slow corner. Give such a macro a Liberty per corner.

Rules:

- `corner` and `corners` are mutually exclusive.
- An empty list is an error.
- Each name must be declared in the PDK's `corners:` and appear once.
- Names may contain only letters, digits, `_`, `.` and `-`, because each becomes an OpenSTA corner name and part of a report file name.
- A one-entry list is the same run as `corner:` with that name.
- A platform that sets neither key uses the PDK's first corner.

## Tune the process-dependent steps

Placement, clock-tree and power-grid steps default to values calibrated for Nangate45. Another process declares its own values. Every key below is optional; a config that sets none of them gets the Nangate45 behaviour.

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
    rcx-rules: pdk/sky130hd/rcx_patterns.rules

cfg-pnr-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    cts-buffer: [sky130_fd_sc_hd__clkbuf_4, sky130_fd_sc_hd__clkbuf_8]
    placement:
      density: 0.6         # this platform only; padding stays the PDK's 2
    dont-use-cells:        # added to the PDK's list, never replacing it
      - "sky130_fd_sc_hd__lpflow_*"
```

- **`placement.density`** is the `global_placement` target utilization, greater than 0 and at most 1. **`placement.padding`** is cell padding in sites, applied to both sides in global placement only; detailed placement runs unpadded. Both belong on the PDK. A P&R platform can override either one field by field, so a platform that names only `density` keeps the PDK's `padding`.
- **`placement.macro-halo`** (default 20.0 µm) is the minimum channel the [macro packer](#macro-placement) keeps between two macros and between a macro and each core edge. Below about 19 µm on sky130hd, `pdngen` cannot repair a channel and fails with `[ERROR PDN-0179] Unable to repair all channels`; a 12 µm channel measurably fails. Raise it for a coarser grid or when macros need routing room. Lower it only when macros do not fit, at the cost of PDN repairability.
- **`placement.macro-cell-halo`** (default 1.0 µm) keeps standard cells off the macros. After macro placement the flow puts a hard placement blockage over each macro, grown by this distance on every side. Without it the detailed placer can abut a cell to a macro edge, and the detailed router then reports a Metal Spacing violation at a pin on a hardened block's edge. `0` places no blockage.
- **`cts-buffer`** takes one name or a list. With a list, every entry becomes the CTS `-buf_list` and the first becomes `-root_buf`, so put the root buffer first.
- **`pdn-config`** is a path, resolved from `root_config.yaml` like the PDK's other paths, to a Tcl snippet that declares the grid (`add_global_connection`, `set_voltage_domain`, `define_pdn_grid`, `add_pdn_stripe`, `add_pdn_connect`). The flow sources it after macro placement and calls `pdngen` itself, so the snippet must not. With the key unset, no PDN Tcl is emitted, as on Nangate45. A named file that is missing fails the run at `setup`, before OpenROAD starts.
- **`dont-use-cells`** and **`rcx-rules`** have their own sections: [Exclude cells with dont-use-cells](#exclude-cells-with-dont-use-cells) and [Extract parasitics with rcx-rules](#extract-parasitics-with-rcx-rules).

## Exclude cells with dont-use-cells

`dont-use-cells` is a list of cell names or `*`-patterns written on the PDK. `rb pnr` and `rb synth` both read it.

- P&R emits `set_dont_use` before the floorplan, so no placement, repair or CTS pass can choose a listed cell.
- Synthesis passes the same patterns to Yosys (`dfflibmap -dont_use`, `abc -dont_use`) and, on the OpenROAD backend, to the resynthesis stage's `set_dont_use`.
- Write one pattern per list entry. Only the `*` and `?` wildcards are allowed. An entry containing whitespace, or a Tcl metacharacter (`[ ] { } $ " \ ;`), is rejected when the config loads.
- Matching is each tool's own: OpenROAD and `dfflibmap` take simple globs and `abc` forwards each pattern to its library exclusion. Check that the routed netlist is free of the cells you meant to exclude.
- A synth or P&R platform can add its own list. It is appended to the PDK's, PDK entries first with duplicates dropped, so a platform can exclude more than its PDK but never less. A platform's list reaches only its own flow; the PDK's reaches both.

With any cell excluded, the flow also checks the routed design after hold repair and detailed routing, before fill insertion and before any output is written. Each placed instance whose master matches a pattern is printed to `pnr.log` as `RB-DONT-USE-VIOLATION: <instance> <master> <pattern>`, and the run fails naming the first one. This catches a cell the synthesis netlist already instantiated, which `set_dont_use` alone does not prevent. An `xfail:` marker on the run can still excuse it.

A pattern that matches no Liberty cell excludes nothing. OpenROAD reports `[WARNING STA-0122] cell '<pattern>' not found.` (or `STA-0121` for the library half of a `lib/cell` pattern), and rb logs `pnr.dont_use_unmatched` naming the pattern. Check it for typos.

## Extract parasitics with rcx-rules

`rcx-rules` is a path, resolved like `pdn-config`, to an OpenRCX extraction-rules file (ORFS `RCX_RULES`). It must match the technology LEF's routing stack (the file's `LayerCount`).

With it set, the flow runs extraction after fill insertion, writes `<top>.routed.spef`, and reads that SPEF back in place of `estimate_parasitics -global_routing`:

```tcl
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file <rcx-rules>
write_spef $OUT_DIR/${DESIGN}.routed.spef
```

- The final `report_worst_slack`, `report_tns` and `timing.rpt`, and so the summary's WNS and TNS, are timed on the extracted parasitics.
- A [`netlist-source: pnr` power run](power.md#extracted-parasitics) reads the same SPEF.
- A named rules file that is missing fails the run at `setup`, before OpenROAD starts.
- With the key unset there is no extraction and the reports use the global-route estimate.

## Macro placement

A netlist that instantiates hard macros, such as an SRAM or a partition hardened by an earlier P&R run, gets automatic macro placement before the power grid is built. To use OpenROAD's placer instead, see [RTL-MP macro placement](#rtl-mp-macro-placement).

The packer works as follows:

- Macros are sorted tallest first, then widest, then by name, and packed left to right into rows. A row is as tall as its tallest macro.
- `placement.macro-halo` is kept between neighbours and at every core edge.
- Each origin is snapped up to the standard-cell site grid counted from the core corner, so snapping only ever widens a channel. The instance is left `FIRM`, which global and detailed placement respect.
- Rows start at the core's lower-left corner, or at the corner [`floorplan.macro-anchor`](#floorplan-controls) names. A single macro therefore sits in the corner, a halo from both edges.

Packing by footprint lets a mixed-size set of macros fit a floorplan sized for the design.

When the macros do not fit, the error states what was tried, the largest footprint, the macro area against the core area, and the smallest core at the run's aspect ratio that the packer would fit:

```
3 macros do not fit the 500.000 x 500.000 um floorplan core
  tried: shelf packing, tallest macro first, keeping a 20.000 um halo (placement.macro-halo) at every core edge and between macros
  macros: largest footprint 479.780 x 397.500 um, 208506.6 um2 of macro area in a 250000.0 um2 core
  smallest core at this aspect ratio that would fit: 556.440 x 556.440 um
  lower the floorplan utilization, change its aspect ratio, or lower placement.macro-halo
```

## RTL-MP macro placement

`floorplan.macro-placement: rtl-mp` hands the macros to OpenROAD's hierarchical macro placer, `rtl_macro_placer`, instead of the packer:

```yaml
    floorplan:
      utilization: 0.45
      macro-placement: rtl-mp     # default pack
```

RTL-MP clusters the netlist and places macros by connectivity and wirelength, rotating them where that helps. Macros end up `LOCKED` and snapped to the track grid. Reports go to `artefacts/<run>/rtlmp/`.

- **Halo and blockages.** It keeps `placement.macro-halo` around every macro and keeps macros out of every placement blockage, including `soft` and `partial` ones that the packer lets macros sit on. Pins, which the flow places later, are treated as clusters on the boundary.
- **Cell halo and power grid.** [`macro-cell-halo`](#tune-the-process-dependent-steps) still applies afterwards, and the power grid is built around wherever RTL-MP put the macros.
- **When to use it.** The packer places macros where their sizes allow; RTL-MP places them where the netlist wants them. On the project template's sky130hd assembly it gave about 0.8 ns better setup slack than the packer (+2.77 against +2.00 ns) at 0 DRCs. It also suits a flat netlist with many memories.
- **Constraints.** Setting `macro-anchor` with `rtl-mp` is a configuration error. RTL-MP's placement changes when the netlist does, so use the packer to pin a macro whose neighbour's pin plan depends on its location.
- **Hardened blocks.** `macro-placement: rtl-mp` is part of a hardened block's configuration digest, so switching placer makes the block's abstract stale.

## Floorplan controls

Two optional `floorplan` keys steer where macros and standard cells may go.

```yaml
    floorplan:
      utilization: 0.45
      core-margin: 10.0
      macro-anchor: upper-right        # default lower-left
      blockages:
        - rect: [10, 10, 130, 60]      # microns, die coordinates: x0 y0 x1 y1
        - rect: [300, 10, 360, 200]
          type: soft
        - rect: [400, 300, 520, 420]
          type: partial
          max-density: 0.4
```

**Macro anchor.** `macro-anchor` is the core corner the [macro packer](#macro-placement) starts from: `lower-left` (default), `lower-right`, `upper-left` or `upper-right`. Rows fill away from that corner, so the two opposite edges stay clear of macros as long as the macros allow. Point `pin-constraints` at those edges when a neighbouring block abuts one side. It is set per run, not per platform, because it is part of the design's pin plan.

**Placement blockages.** Each `blockages` entry is `rect: [x0, y0, x1, y1]` in microns, in die coordinates with the die's lower-left corner as origin, plus a `type`:

| `type` | Effect |
| --- | --- |
| `hard` (default) | No standard cell is placed inside. The packer moves any overlapping macro along its row past the blockage and skips a row with no room. No halo is kept to a blockage. |
| `soft` | Global placement keeps cells out; later repair and legalization may use the area. Macros may sit on it, except under [`rtl-mp`](#rtl-mp-macro-placement). |
| `partial` | Global placement caps cell density inside at `max-density`, a fraction strictly between 0 and 1. Only global placement honours the cap: the detailed placer treats it as fully blocked, so legalization moves the cells out and the area behaves like `hard` for standard cells. Macros may sit on it, except under `rtl-mp`. |

The flow creates blockages with `create_blockage` right after the floorplan, before macro and global placement. `create_blockage` needs OpenROAD 26Q1 or newer; a run with blockages fails at setup on an older build.

These fail when `pnr.yaml` loads: a malformed rectangle (`x0 >= x1`, `y0 >= y1`, a side under 0.001 µm, a negative or non-finite coordinate, or not four numbers), an unknown type, and `max-density` on anything but `partial`. A rectangle outside the die fails in OpenROAD. When hard blockages leave macros no room, the no-fit error reports how many blockages it avoided and lists moving one among the fixes.

## PDK setup notes

The same flow runs on any PDK the root config declares; switching PDKs is a `platform:` change in the run YAMLs. What the three open PDKs need:

- **Nangate45 (FreePDK45).** All flow defaults are calibrated on it. Set `site`, one Liberty corner, `tech-lef`, `macro-lef`, the tie and fill cells and `cts-buffer: BUF_X4`, and leave every process key unset. It has no PDN snippet, so runs have no power grid. The template's `synth/demo_tiny_alu_subsys/download_pdk.sh` fetches the views.
- **Nangate45 parasitics.** The template leaves `rcx-rules` unset, so runs use global-route estimates. For extraction, point `rcx-rules` at ORFS' `flow/platforms/nangate45/rcx_patterns.rules`. Its LEF has no via resistance, so OpenRCX extracts vias as 0 Ω; rb does not set layer RC.
- **sky130hd (SkyWater 130 nm, high-density cells).** The template's `sky130hd` PDK in `root_config.yaml` is a worked entry, used by the `demo_tiny_alu_subsys_hier` runs. Its `download_pdk.sh` fetches the Liberty, LEFs, GDS, KLayout files and rules from a pinned ORFS commit. Beyond the Nangate45 fields it needs `pin-layers` (`met3` / `met2`), `routing-layers` in `met*` names, a `pdn-config` (committed under `pnr/sky130hd/`), `placement.density: 0.60`, a `dont-use-cells` list for the probe and `lpflow` cells, and a `cts-buffer` list.
- **sky130hd rules.** Use `flow/platforms/sky130hs/rcx_patterns.rules` for `rcx-rules`; ORFS' sky130hd file is a symlink to it. sky130hd and sky130hs are separate `cfg-pdks` entries because their cell LEFs differ.
- **ASAP7 (7 nm predictive).** Not exercised end to end by rtl_buddy's own validation. An entry would follow ORFS' `flow/platforms/asap7`: site `asap7sc7p5t`, the `asap7_tech_1x_*.lef` technology LEF, pin layers `M4` / `M5`, routing layers `M2-M6` (clock `M4-M6`), tie cells `TIEHIx1_ASAP7_75t_R/H` and `TIELOx1_ASAP7_75t_R/L`, `cts-buffer: BUFx4_ASAP7_75t_R`, `dont-use-cells` such as `*x1p*_ASAP7*`, `*xp*_ASAP7*`, `SDF*`, `ICG*`, a much lower placement density (OpenROAD's test flow uses 0.3 with padding 2), `asap7.pdn.tcl`, and ORFS' `rcx_patterns.rules`.

ASAP7 has three gaps before a clean run:

- Standard cells are split across one Liberty file per cell group (`AO`, `INVBUF`, `OA`, `SEQ`, `SIMPLE`) per Vt and corner, mostly gzipped, while `corners:` takes one Liberty path per corner. Merge each corner's files into one, or list the extra files in each run's `lib-paths`.
- The cell LEF is one file per Vt flavour and `macro-lef` takes one path. List the others in each run's `lef-paths`.
- ORFS runs `tapcell` and sources a tracks file (`asap7.tracks.tcl`) and a layer-RC file (`setRC.tcl`). The rtl_buddy flow does none of these and calls `make_tracks` with the LEF defaults.

## Constrain boundary pins

Set `pin-constraints: pins.tcl` on a run to assign pins to boundary edges or groups. The path is relative to `pnr.yaml`, and a missing file fails the run. Without the key, pin placement is unconstrained.

```tcl
set_io_pin_constraint -pin_names {req_* clk} -region left:*
set_io_pin_constraint -pin_names {rsp_*} -region right:*
```

- The file is sourced immediately before `place_pins`, which runs after [macro placement](#macro-placement) and the power grid and before global placement. Placing pins earlier leaves macros unplaced and draws `PPL-0015 Macro ... is not placed` warnings.
- Do not put these commands in SDC. SDC is read before the floorplan exists, when whole-edge intervals can resolve to zero length.
- Keep clock pin layers compatible with the platform's clock-routing range.
- `place_pins` excludes only the boundary intervals a placed macro actually touches, and the packer keeps every macro a halo inside the core. To keep a pin edge clear of macros, anchor the packer at the opposite corner with [`floorplan.macro-anchor`](#floorplan-controls).

## Keep stage checkpoints

A routing run that hits a scheduler wall limit leaves only its log, because the flow writes its DEF and ODB after detailed routing. Set `checkpoints:` on the run to keep a database at each stage boundary and a progress file that says where the run is:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    checkpoints: true            # or a stage, or a list: [cts, global_route]
```

| Stage | Written | Holds |
| --- | --- | --- |
| `floorplan` | before `global_placement` | floorplan, pins, tie cells, placed macros, PDN |
| `place` | before `clock_tree_synthesis` | legalized global placement after `repair_design` |
| `cts` | before `global_route` | clock tree, hold repair, legalization, `check_placement` |
| `global_route` | after a successful `global_route` | the global route: ODB, route guides and route segments |

Each stage writes `<NN>_<stage>.odb`, `.def` and `.sdc`; `global_route` also writes `.guide` and `.segments`. `true` asks for all four stages; a name or list asks for some. `checkpoints: []` keeps only the progress file and manifest. Unset or `false` disables the feature.

```
artefacts/<run>/checkpoints/
  latest -> 20260925T101500-4242
  20260925T101500-4242/
    manifest.json      # inputs, hashes, OpenROAD version, outcome
    progress.jsonl     # one JSON event per line, appended as the flow runs
    01_floorplan.odb  01_floorplan.def  01_floorplan.sdc
    ...
```

- **Progress.** `progress.jsonl` gets a `step_begin` and a `step_end` (`ok` or `error`, with the error text and elapsed time) for every flow command, and a `checkpoint` event once all of a stage's files are on disk. Lines are flushed as written, so `tail -f artefacts/<run>/checkpoints/latest/progress.jsonl` follows a running flow. After a kill, the last `step_begin` with no `step_end` is the step the run was in.
- **Manifest.** `manifest.json` is written before OpenROAD starts, with SHA-256 fingerprints of the netlist, SDC, Liberty and LEF files, pin-constraints and PDN snippets, the generated `pnr.tcl`, and the OpenROAD path and version. Afterwards it gains the outcome, the step the run stopped in, and a fingerprint of every checkpoint file.
- **Never final.** A checkpoint is never named `*.routed.*` and never sits where `rb power` or a plain `rb pnr-export` looks. Every manifest entry says `final: false`, `detail_routed: false` and whether it is `global_routed`, and `congestion.available: false` with the reason, since no checkpoint carries a congestion grid.
- **Survives failure, never reused.** Each run writes into a new `<timestamp>-<pid>` directory and never touches an earlier one. Every `rb pnr` run removes `latest` first, and a checkpointed run points it at its own directory once OpenROAD launches. Old directories are not pruned; delete the ones you no longer need.

Resume from a checkpoint is not supported. To look at one, open its ODB in OpenROAD or stream it out with [`rb pnr-export --checkpoint`](#export-a-saved-result).

## Harden a block

A block that a larger design instances as a hard macro needs three views of its routed result: an abstract LEF for placement and routing, a Liberty timing model for synthesis and STA, and its layout for stream-out. Set `harden: true` on the block's run to publish them beside its routed outputs:

```yaml
runs:
  - name: alu_block_pnr
    # ...
    platform: sky130hd_tt_block
    harden: true
```

```
artefacts/<run>/abstract/
  <top>.lef                 write_abstract_lef -bloat_occupied_layers
  <top>.lib                 write_timing_model (OpenSTA)
  <top>.gds                 the run's strict stream-out
  abstract.manifest.json    fingerprints of every input and output
```

- **Same session.** The P&R run writes the LEF and Liberty model after `write_db`, so the model uses the Liberty set the block was routed against and the propagated clocks CTS left. `-bloat_occupied_layers` marks every layer the block routes on as blocked over its whole footprint.
- **Strict layout.** `harden` implies `--gds` and forces `gds-mode: strict`, whatever the run or `--gds-mode` says. It therefore needs KLayout.
- **All or nothing.** Views are staged in `abstract.partial/` and moved into place only when all three and the manifest exist. If one is missing, the run fails with `fail_stage: abstract` and leaves no abstract directory. The routed DEF and ODB stay. Every rerun removes the previous abstract first.
- **One corner.** An abstract carries one corner's timing model, so `harden` on a multi-corner platform is refused before OpenROAD starts.
- **No power.** `write_timing_model` writes timing arcs only. A parent's `rb power` sees a hardened block as drawing 0 W, so run `rb power` on the block itself.

`abstract.manifest.json` (`schema_version: 1`) records the block, run, platform, PDK, OpenROAD path and version, the technology LEF and corner Liberty it was built on, and a `{path, size, sha256}` fingerprint of each input and output. Inputs are the RTL sources of the upstream synthesis filelist, the netlist, the SDC, every Liberty and LEF, and the pin-constraint and PDN snippets. A `config` section records the configured files, floorplan, placement, routing layers, CTS buffers and don't-use cells, with a digest over them.

## Block power-grid convention

A parent ties a hardened block into its grid the way it ties in any macro: its top-level straps cross the block and drop vias onto the block's power pins. This works only if the block leaves the top layers free. rtl_buddy does not enforce the split, so give the block its own platform that does:

- **Layers.** The block owns the lower layers and the parent owns the top ones. On sky130hd the block routes and straps on met1 to met4 and exposes power straps as pins on met4, and the parent's met5 straps run over it. With `-bloat_occupied_layers`, one block strap on met5 blocks met5 over the whole block and the parent's `pdngen` fails.
- **Block PDK entry and platform.** Point a copy of the PDK entry at a block-level `pdn-config` whose straps stop below the parent's layers, and set the block platform's `routing-layers` to the same range.
- **Third-party macros.** An OpenRAM SRAM exposes power on met4 and met3. A block that does the same looks identical to it from the top.

The project template's sky130hd hierarchical example uses this split: see `pnr/sky130hd/pdn_block.tcl` and the `sky130hd_tt_block` platform.

## Assemble hardened blocks

A top-level run instances hardened blocks by naming them under `blocks:`, in `pnr.yaml` and in the `synth.yaml` entry the run reads:

```yaml
# pnr.yaml
runs:
  - name: top_pnr
    synth: top_synth
    synth-path: ../../synth/top/synth.yaml
    platform: sky130hd_tt
    blocks:
      - name: alu_block          # module name as instanced in the top netlist
        pnr: alu_block_pnr       # a harden: true run
        pnr-path: ../alu/pnr.yaml  # default: this pnr.yaml

# synth.yaml
syntheses:
  - name: top_synth
    # ...
    blocks:
      - name: alu_block
        pnr: alu_block_pnr
        pnr-path: ../../pnr/alu/pnr.yaml   # required here
```

Each entry resolves to the `abstract/` its `harden: true` run published. P&R appends the abstract's `.lef`, `.lib` and `.gds` to the run's `lef-paths`, `lib-paths` and `gds-paths`; synthesis appends the `.lib` and `.lef` to its own lists. The result's `blocks` field lists each consumed block with its run, abstract directory, manifest, the `{path, size, sha256}` fingerprints of the three views, and whether it was stale.

- **Build the blocks first.** A named run never starts a block's run. With no abstract, the consuming run fails before its tool starts, naming the block and the `rb pnr` command that builds it. Synthesis of the top reads the block's Liberty model, so it also runs after the block is hardened. `rb pnr` with no run name orders this for you; see [Run a whole hierarchy](#run-a-whole-hierarchy).
- **Same technology and corner.** A block may be hardened on its own block-level platform, but its technology LEF and corner Liberty must match the consuming run's by content, or the run fails with a platform/corner mismatch. A multi-corner P&R platform cannot consume single-corner abstracts.
- **Stale abstracts are refused.** Before the tool starts, every input in the block's manifest and the three published views are fingerprinted again, and the block's configuration is rebuilt from its `pnr.yaml` and platform and compared by digest. Timestamps are never used. Any difference fails the consuming run, naming the block, what changed, and the `rb pnr` command that re-hardens it.
- **Accepting a stale abstract.** `--accept-stale` on `rb pnr` or `rb synth` consumes it anyway. The result description then says `stale block abstract(s) accepted: <names>`, and that block's row has `stale: true` and the list of changes.
- **Blackbox the module.** `blocks:` supplies views only. The top's filelist must still leave the block's module a blackbox, typically a port-only stub, as for any hard macro.
- **Power.** The abstract Liberty has no power data, so a parent's `rb power` reports the block as drawing nothing (see [Harden a block](#harden-a-block)).

## Run a whole hierarchy

`rb pnr` with no run name runs every entry in the `pnr.yaml`, each block's run before the runs that consume it:

- **Order.** Runs are sorted by their `blocks:` edges. Where the edges leave a choice, runs keep file order, so a `pnr.yaml` without `blocks:` runs in file order.
- **Other files.** A block whose `pnr-path` is another `pnr.yaml` is pulled into the plan ahead of its consumer, along with its own blocks. A pulled-in run writes to its own suite's `artefacts/`, takes that tree's lock, and its result row carries a `suite` key.
- **A failed block.** A run whose block failed, including an expected failure under `xfail`, is not attempted. It is reported as `FAIL` with `fail_stage: blocked`, a description naming the block, and a `blocked_by` list. An `xfail` marker does not excuse it. Runs independent of the failed block still run, and blocking propagates through any number of levels.
- **A skipped block.** A block skipped by `-l` does not block its consumer, which uses the abstract already published. A run that `-l` deselects is `SKIP` whatever its blocks did.
- **A crash.** A run that crashes rather than failing reports `FAIL` with `fail_stage: error` and the exception in its description, blocks its consumers, and leaves every other run's row intact.
- **Configuration errors.** A cycle in `blocks:` (`pnr blocks: cycle: a -> b -> a`), a block naming a run its `pnr.yaml` does not define, a missing `pnr-path`, or two runs that would write the same `artefacts/<run>` directory stop the command before anything runs.

## Build a hierarchy from a clean tree

Ordering P&R alone is not enough on a clean tree, because the top's synthesis reads the blocks' abstracts. `--synth` runs each P&R run's upstream synthesis just before it, so the whole hierarchy builds in one command:

```sh
rb pnr -c pnr/top/pnr.yaml --synth
```

- **Synthesis.** A synthesis runs once however many P&R runs read it, regardless of the synthesis entry's own `reglvl`. A run also waits for the blocks its synthesis entry names under `blocks:`, pulled in like the run's own. Every synthesis in the plan is resolved before the first run starts, so a misspelt `synth:` stops the command up front.
- **Synthesis failure.** A synthesis that does not pass fails its P&R run with `fail_stage: synth`, which blocks that run's consumers. Each P&R row carries the synthesis it ran as `synth` (`name`, `suite`, `result`, `desc`). `--accept-stale` applies to the syntheses too.
- **Parallel runs.** `-j N` runs up to `N` P&R runs at once. Each starts, in plan order, as soon as every block it names has finished, so independent blocks harden side by side. Results are reported in plan order, and a shared synthesis still runs once. Every artefact tree in the plan is locked before the first run starts. The default is `-j 1`.
- **Threads.** Each run is a whole OpenROAD session with its own [`threads:`](#openroad-threads), so size the two together.

## Run P&R

```bash
rb pnr --list -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds-mode strict
```

`--png` and `--gds-mode` imply `--gds`. RTL Buddy invokes KLayout after a successful OpenROAD run. In the default `preview` mode, a KLayout failure gives a warning and leaves the P&R verdict unchanged; use the OpenROAD timing and DRC results as the outcome. In `strict` mode, an export that could not be delivered fails the run; see [Stream-out completeness](#stream-out-completeness).

## OpenROAD threads

OpenROAD runs on one thread unless the run asks for more; reserving CPUs from a scheduler does not change that. Set `threads:` on the run:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: 8        # or: auto
```

| Value | Threads OpenROAD is given |
| --- | --- |
| unset | 1, OpenROAD's default |
| a positive integer | That many, emitted as `set_thread_count N` before the first `read_liberty` |
| `auto` | The CPUs of the allocation the run is in; 1 when there is none. Never the host's core count |

Zero, negative numbers, booleans, quoted numbers and other strings fail configuration loading. `0` is refused because OpenROAD reads it as every core on the host.

`rb pnr` is not dispatched. Run it inside an allocation (`srun -c 8 rb pnr ...` or an `sbatch` script) to give OpenROAD CPUs. The allocation is the smaller of:

- inside a Slurm job (`SLURM_JOB_ID` set), `SLURM_CPUS_PER_TASK`, else `SLURM_CPUS_ON_NODE`;
- a CPU affinity mask smaller than the machine (`taskset`, a cpuset cgroup or a bound Slurm step; Linux only).

A count above the allocation is clamped to it with an `openroad.threads_capped` warning naming both numbers. It is a warning so one checked-in `pnr.yaml` works in allocations of any size. Outside an allocation an explicit count is used as given, up to OpenROAD's own limit of the host's hardware threads. A container CPU quota (`docker --cpus`) is not visible to either check.

The result carries `openroad_threads` with the `requested` value, the `effective` count, and the `allocation` and its `allocation_source`. `effective` is the count OpenROAD reported (`[INFO ORD-0030] Using N thread(s).` in `pnr.log`), else the count RTL Buddy set. The same key and contract apply to [`rb power`](power.md) and to the OpenROAD stage of [`rb synth`](synthesis.md). The thread count is not part of any result fingerprint, and results are not guaranteed bit-identical across thread counts. Compare DRC and slack yourself if it matters.

## Stream-out inputs

Stream-out reads more than the routed DEF.

- **Layout.** It comes from the PDK's `cell-gds` (one path or a list), then the run's `gds-paths`. A hard macro's layout belongs in `gds-paths`; an OpenRAM SRAM has its LEF in `lef-paths` and its GDS in `gds-paths`. Each path resolves against the file that names it, `root_config.yaml` for the PDK and `pnr.yaml` for the run.
- **LEFs.** The DEF reader gets the technology LEF, the PDK's macro LEF, then the run's `lef-paths`, de-duplicated, appended to whatever the KLayout technology file already lists.
- **Record.** Both lists reach KLayout through `def2stream.inputs.json` in the artefact directory, which shows what a given run streamed.

An input that the config names and the disk lacks stops the export before KLayout starts, with every missing path reported at once.

## Stream-out completeness

A cell that the DEF instantiates but no GDS file contains is streamed as an empty placeholder. Sometimes that is intended, such as an ORFS `fakeram45` macro that exists only in LEF. Sometimes a design forgot its real SRAM GDS and got a picture with a hole in it. `gds-mode` says which the run means:

```yaml
runs:
  - name: demo_pnr_signoff
    # ...
    gds-mode: strict          # default: preview
    gds-allow-empty:          # cells that are empty on purpose
      - fakeram45_*
```

- **`preview`** (default) keeps the layout and reports what is missing: `pnr.gds_incomplete` at WARNING naming every cell, `gds_status: incomplete` with `gds_missing_cells` and `gds_missing_cell_count` in `--machine` output, `GDS incomplete: …` in the run description, and `gds+png (incomplete: N missing)` in the summary's Outputs column. The run still passes.
- **`strict`** refuses to publish an incomplete layout. Each of the following makes the export a `FAIL` with `fail_stage: export`: cells with no layout, a missing KLayout executable, a PDK with no `klayout-tech`, a configured input off disk, any other stream-out failure, and a failed `--png` render. The GDS, PNG and stream-out report are removed. The routed DEF, netlist, SDC and ODB stay, since OpenROAD finished cleanly. An `xfail:` marker does not excuse an export failure.

`gds-allow-empty` takes cell names or case-sensitive `fnmatch` globs. The `GDS_ALLOW_EMPTY` environment regex is still honoured. A covered cell is not missing in either mode; it is listed in `gds_allowed_empty_cells` and counted as `(N empty by design)` in the Outputs column.

Completeness comes from `def2stream.report.json`, which the bundled KLayout helper writes after the layout, not from KLayout's console output. A run whose report is absent, unreadable or from another schema has a failed export, because nothing vouched for the layout.

## Export a saved result

KLayout, a PDK's GDS or the layer properties often arrive after the P&R does. `rb pnr-export` streams an existing routed result out again, DEF to GDS to PNG, without rerunning synthesis or OpenROAD:

```bash
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds-mode strict
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --def ../saved/demo_top.def
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --checkpoint cts --png
rb pnr-export demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png-only --lyp dark.lyp --png-width 4096 --png-height 4096
```

Run selection, `-c`, `-l` and `--gds-mode` work as for `rb pnr`, and the export reads the same configuration: technology, the PDK's `cell-gds`, the run's `gds-paths` and `lef-paths`, and `gds-allow-empty`. The design name comes from the upstream `synth:` entry in `synth.yaml`; the export needs the synthesis configuration and none of its artefacts. `rb tool-check --required-for pnr-export` asks for KLayout and nothing else.

Before anything launches, the saved result is checked. The routed DEF must exist, be non-empty, and declare the design the run's synth entry names, by its `DESIGN <top> ;` statement. A mismatch fails with both names. A missing layer properties file, a PDK with no `klayout-tech`, a configured input off disk and a missing KLayout each stop the export the same way. File timestamps are never used.

The export clears only what an export publishes: the GDS, PNG, `def2stream.report.json`, `def2stream.inputs.json` and its own record. The routed DEF, ODB, netlist, SDC and P&R reports stay untouched, even when the export fails.

- **`--def <path>`** exports a DEF from elsewhere, with the platform and top still taken from the run. It needs a single named run.
- **`--checkpoint`** exports a [stage checkpoint](#keep-stage-checkpoints) instead of the routed result. It needs a single named run and excludes `--def`. Give a stage name (`cts` or `03_cts`) to use the `latest` run, `<run-id>/<stage>` for an older one, or a path to one of a checkpoint's files. The checkpoint needs a completed `checkpoint` event in its `progress.jsonl`, so a database a kill interrupted mid-write is refused.
- **Checkpoint outputs** go to `checkpoints/<run-id>/export/<NN>_<stage>/`, never the routed layout's paths. The row carries `checkpoint_stage`, `checkpoint_run_id` and `checkpoint_final: false`, its description says the checkpoint is not final, and the record gains a `checkpoint` block with the manifest's `final`, `global_routed` and `congestion` labels.
- **`--png-only`** re-renders the PNG from the GDS already in the artefact directory. It does no stream-out and never rewrites the GDS. `--lyp`, `--png-width` and `--png-height` change how it is drawn. If a `def2stream.report.json` beside the GDS says cells had no layout, the re-render carries the same qualifier. With no report beside it, the re-render is reported as qualified rather than complete.

## The export verdict

For `rb pnr`, the export is an extra over the P&R verdict, so in `preview` mode a failed export leaves the run passing. For `rb pnr-export` the export is the whole job:

- Everything requested is on disk: `PASS`, exit 0.
- A `preview` layout published with cells that have no GDS: `PASS` with the `GDS incomplete: …` qualifier, exit 0.
- Anything else, in either mode: `FAIL` with `fail_stage: export` and exit 1. This covers no KLayout, no technology, an input off disk, a stream-out that wrote nothing, a failed `--png` render, and under `strict` a layout with cells that have no GDS.

Rows carry the same fields as an `rb pnr` row (`gds_status`, `gds_mode`, `gds_missing_cells`, `gds_missing_cell_count`, `gds_allowed_empty_cells`, `gds_path`, `png_path`) plus `export_provenance`.

## The export record

Each export writes `export.provenance.json` into the artefact directory, whatever the outcome. The exception is a failure so early that the design cannot be named, which writes none and clears the previous one. It never edits `pnr.log`, `pnr.tcl`, the reports or the P&R run's results. A fresh `rb pnr` run clears it.

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

Paths are project-relative POSIX where possible. Exactly one of `inputs.def` and `inputs.gds` is set: the DEF a stream-out read, or the GDS a re-render used. It carries the size and SHA-256 of the bytes read, which a later reader compares to decide whether the layout still belongs to the result beside it.

## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing, and zero DRC lines indicate a clean route.

A run passes when OpenROAD exits 0 and emits no `[ERROR ...]` line and, with `gds-mode: strict`, the requested export was delivered complete. It skips when filtered by `reglvl` or when `tool:` is unsupported. Timing violations and DRC counts are reported as metrics; decide in your project whether to gate signoff on them.

On a multi-corner platform, `wns_setup_ps`, `wns_hold_ps` and `tns_ps` are the worst across all corners, so summaries, gates and `xfail` markers read them unchanged. The result also carries:

- `worst_setup_corner` and `worst_hold_corner`, the corners that set each worst value. The summary shows them in a `Worst Corner` column.
- `corners`, each corner's own `wns_setup_ps`, `wns_hold_ps` and `tns_ps` in config order.

The per-corner values are also in `pnr.log` after `>>> Per-corner timing`. `timing.rpt` shows the worst path across all corners.

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
| `<top>.routed.spef` | OpenRCX-extracted parasitics; only when the PDK sets `rcx-rules` |
| `timing.rpt` | Expanded worst-path timing |
| `route.drc.rpt`, `route.maze.log` | DRC summary and detailed-route log |
| `<top>.gds`, `<top>.png` | Optional KLayout outputs |
| `export.provenance.json` | What an `rb pnr-export` invocation read and produced |
| `klayout.*.log` | Optional conversion logs |
| `checkpoints/` | Optional [stage checkpoints](#keep-stage-checkpoints), one directory per run, plus `latest` |
| `abstract/` | Optional [hardened-block abstract](#harden-a-block): LEF, Liberty, GDS and manifest |

A failed or partial run never leaves an earlier run's outputs beside its own. `rb power` resolves `<top>.routed.odb` by path and must not be handed a stale database.

- **Up front.** Every file above except the logs, including the optional KLayout outputs, is deleted before each run, even when OpenROAD is missing. A run that dies short of routing leaves the outputs it never wrote absent.
- **After `write_db`.** A run that reaches `write_db` and then dies (killed, non-zero exit, or an `[ERROR ...]` line) has its outputs removed again, so a `FAIL` never leaves a routed database or SPEF behind.
- **`pnr.tcl` and `def2stream.inputs.json`** are cleared only up front. A run that reaches the tools keeps them even when it fails, because `pnr.log` and `klayout.def2stream.log` describe them.
- **KLayout steps.** A zero-length GDS, a half-rendered PNG and a stream-out report that would call the layout complete are removed. A `strict` export failure removes the layout and its report and keeps the routed outputs.
- **`checkpoints/`** is exempt. Its run directories are never cleared; only `latest` is removed up front.
- **`rb pnr-export`** clears only the layout, image, stream-out report, input manifest and its own record.

On failure, read `pnr.log`. If only KLayout failed, read the matching `klayout.*.log`, fix the installation, and rerun with `--gds` or `--png`.
