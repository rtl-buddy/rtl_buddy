---
description: Query saved synthesis and power artefacts by module and instance with rb phys or the hub's synth+power pane, from the physical model each run writes.
---

# Physical Metrics

`rb phys` answers per-module and per-instance questions about a design's area, cells and power from the physical model that `rb synth` and `rb power` write. It runs no tools and writes nothing. The hub's synth+power pane shows the same model.

## Read the model a run produced

Every synthesis and power run writes `phys-model.json` and `phys-manifest.json` into an artefact directory: its own, or the one a power run's `phys-run:` names. Without an override, `rb phys` reads the newest `phys-manifest.json` under the project root.

```bash
rb phys summary
rb phys summary --limit 0 --instances-limit none
rb phys module sub
rb phys instance u_sub
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

- `summary` reports the run header, design totals, the heaviest modules by cell count and the hottest instances by total power. `--limit 0` shows every row. `--modules-limit` and `--instances-limit` override `--limit` for one ranking each and accept `none` to drop a ranking. A mapped design has hundreds of thousands of leaf instances, so `--limit 0 --instances-limit none` gives the full module table without them.
- `module` reports one module's cells and area, then its instances and their power. The name may be an RTL module (synthesis half) or a Liberty cell (power half); see [What the module join can answer](#what-the-module-join-can-answer).
- `instance` reports one instance's power. A path that names a subtree lists the leaves under it, hottest first, and rolls them up. `u_sub.u_leaf` and `u_sub/u_leaf` name the same instance.

`--phys-dir` selects an artefact directory and `--manifest` names the manifest directly. `--manifest` wins over `--phys-dir`, which wins over discovery.

## List the runs a project holds

A project accumulates one artefact directory per partition, corner and power mode. `rb phys runs` lists them, newest first (`--limit 0` for all):

```bash
rb phys runs
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

Each row shows the run name and top, the backends, the power mode and activity source, a configuration fingerprint, the `rb xplr` experiment when the run sits under `artefacts/xplr/<exp-id>/`, and the generation time. The row marked `*` is the newest, which the other verbs read by default.

The artefact directory of each run is printed under the table, ready to pass to `--phys-dir`. An unreadable manifest is listed with its error. A project with no physical artefacts lists nothing and exits 0.

## Tell two runs apart

Both producers record what shaped a run beside what it measured, so two runs of one design are two measurements. A model written by an older rtl-buddy lacks these fields and shows them as not recorded.

- **Power mode and activity.** `rb power` records its mode (`static` or `dynamic`) and its activity source: `default`, `synthetic`, `saif` or `vcd`. For a trace it also records the path, sha256, the test that produced it (derived from the artefact layout) and the scope. For `synthetic` it records the toggle rate and duty. A trace re-captured under the same path has a different sha256 and counts as a different measurement.
- **Config fingerprint.** Both flows record the platform, effort, constraints file and its sha256, and a short digest of the effective tool options, for example `nangate45 · timing-opt · sdc a1b2c3d4 · opts 5655beea1f20`. The digest covers only settings the backend uses, so equivalent spellings match. Power runs also identify the netlist or routed database they measured.

## Read a model with only one half

A synthesis fills the model's `modules` half and a power run fills its `instances` half. The halves meet in one artefact directory, for the same top and netlist, in either order. To pair them, set `phys-run: <synth run>` in `power.yaml` so the power run publishes beside that synthesis. See [Pair the model with a synthesis run](power.md#pair-the-model-with-a-synthesis-run), [Synthesis](synthesis.md#inspect-artefacts) and [Power Analysis](power.md#inspect-artefacts).

With one half absent, every verb still answers from the half present and names the command that produces the other.

- The merge requires both halves to record the same netlist sha256. When it refuses, the power run warns and publishes its half alone; rerun the synthesis in that directory to pair them.
- A power half from a routed database (`netlist-source: pnr`) has no netlist to hash and cannot be paired. Synthesise, then rerun `rb power` on the netlist the synthesis wrote.
- `rb phys instance` needs the power half. Without it the verb exits 2 and points at `rb power`.

## What the module join can answer

The two halves use `module` in different namespaces. In the synthesis half it is an RTL module name. In the power half it is the Liberty cell each leaf instance is an instance of, such as `DFF_X1` or `NAND2_X1`.

`rb phys module` therefore answers Liberty-cell questions: `rb phys module DFF_X1` reports every flop instance and their combined power.

It cannot attribute power to an RTL module such as `u_cpu`, because no leaf row carries that name. When a name resolves in the synthesis half only, the console says so. An empty instance list does not mean the block burns nothing; use `rb phys instance u_cpu`, which sums the leaf rows under that path. See [Roll up a hierarchy](#roll-up-a-hierarchy) and [Known Issues](../known-issues.md#rb-phys-module-reports-no-power-for-an-rtl-module).

A name can exist in both namespaces, such as an RTL module called `DFF_X1`. `rb phys module` then lists both in `namespaces`, prints a collision note, and reports the module row and the cell instances without adding them together.

## Roll up a hierarchy

The model stores leaf values only. `rb phys instance <path>` sums the leaves under the path at query time.

- `match` is `exact` for a path that names a row and `prefix` for a subtree.
- A path that is both a row and a prefix of other rows rolls up to the row alone. The rows below it are listed and counted in `child_count` for navigation, and the console says they are not in the total.
- The rollup adds the four power columns only. The model has no per-cell area, so area comes from `rb phys module`.

## Browse the model in the hub

Start the browser layer and open `/phy`:

```bash
rb hub start --serve-viewer
```

`GET /phy.json` returns the `rb phys summary` payload with no row limit plus the `rb phys runs` listing, so the pane and the CLI agree. `GET /phy.json?dir=<phys dir>` selects a run.

- **Run dropdown.** Entries read `run · top · backends · mode (activity) · experiment`. The newest run is the default; choose the newest entry to return to following it. Hover shows the artefact directory and fingerprint.
- **Tables.** The pane ranks modules by cells or area and instances by leakage, dynamic or total power, and filters the instance table to a module when you click it. If the clicked name is an RTL module, nothing matches and the pane says so; if it is in both namespaces, the pane prints the collision note.
- **Large tables.** The instance table shows 500 rows at a time (`show more`, `show all`). Sorting, filtering and totals cover the whole set.
- **Totals.** `dynamic` is internal plus switching power, summed in the browser. The header shows the flow's own scraped total beside the sum of the rows and says when they disagree.

See [Hub](hub.md#synthpower-pane) for the routes and the peer contract.

## Focus the pane from another app

Point the pane at a target from anywhere:

```bash
rb hub send phys-focus module:sub --metric area
rb hub send phys-focus instance:u_sub/_64_
```

- An unprefixed target is an instance path. An instance target must name a leaf row; a subtree path (`match: prefix`) selects nothing, so send one of its children.
- The message names a target and a metric, never a run, so it applies to the run the pane shows. The hub replays the latest focus when the pane opens.
- Clicking a module broadcasts `graph_focus` and clicking an instance broadcasts `selection_changed`, which the schematic follows.

The schematic and the pane share paths only when both show the same design. A `/sch` showing a testbench around the DUT, or another design, selects nothing. Open the schematic on the synthesis `top:`. See [Known Issues](../known-issues.md#phys-pane-and-schematic-selections-cross-only-within-one-hierarchy).

## Paint the graph with physical heat

Open `/gph` and tick `heat` to fill the design graph's module nodes with this model's numbers. Cells and area come from the synthesis half by module name. Power is the sum of the leaf rows inside every instantiation of the module, joined by instance path. The pane shows how many instantiations and rows went into each figure. See [Physical Heat on the Graph](graph.md#physical-heat-on-the-graph).

The graph's run dropdown selects the same runs as this pane, and a `phys-focus` turns the overlay on.

## Machine output

`--machine` emits the payload the verb built, each with its own `schema_version`, the project-relative manifest and model paths, the run header and the artefact block.

- **Run identity.** The header carries `power_mode`, `power_activity`, `config` (`synth` and `power` fingerprints with a `summary`), and `xplr` (`{"id", "label"}` or `null`).
- **Halves.** `halves` and `missing_halves` report which halves the model has and which command fills each. `null` means not measured; `0` means measured zero.
- **Module join.** The `module` payload adds `namespaces` (`["rtl"]`, `["liberty"]` or both) and `instance_join`: `null`, or a sentence for the Liberty-cell limit or a name collision.
- **Runs.** `rb phys runs --machine` emits `count`, `limit` and `runs`, newest first. Each entry carries the listing's columns, `phys_dir` (pass it to `--phys-dir`), `manifest`, `newest` and `error`, which is set only for an unreadable manifest.

`--limit` truncates the machine payload as well as the table, and `--limit 0` asks for all rows. On `summary`, the `limits` block reports the limit each ranking used. A ranking set to `none` is an empty list, and `counts` still reports the rows the model holds (`null` means a missing half).

`module` carries `limit` and `instance_count`, and `instance` carries `limit` and `child_count`; these count rows that exist, not rows listed. `module`'s `power` and a prefix `instance`'s `rollup` always cover every row.

## Use phys from MCP

`rb mcp` exposes `phys_runs`, `phys_summary`, `phys_module` and `phys_instance`. They read files directly and need no EDA tool and no running hub. See [The MCP server](graph.md#the-mcp-server).

- `phys_runs` takes `limit` and is where the `phys_dir` for the other tools comes from.
- `phys_dir` and `manifest` are the tool arguments for `--phys-dir` and `--manifest`; a relative path resolves against the project root.
- All four take `limit` with the flag's default (`0` for every row). `phys_summary` also takes `modules_limit` and `instances_limit`, an integer or `"none"`.
- A `phys_focus` tool mirroring `rb hub send phys-focus` joins them when a live hub is discovered.

## Troubleshooting

- **`phys: no phys-manifest.json under <root>` or `in <dir>` (exit 2).** No run has published a model there. Run `rb synth` or `rb power`.
- **Unknown module or instance (exit 2).** The error lists close candidates.
- **`phys: cannot read <file>`, or a manifest or model refused by name.** The JSON is unreadable or has the wrong shape. Rerun the producer. A `null` half is valid.
- **`phys-model.json has no per-module breakdown` (synthesis) or `per-instance breakdown` (power).** The stat dump or instance report was missing. Totals are kept, but `rb phys module` or `instance` has nothing for that half. Rerun the flow.
- **`phys-model.json was not written`.** Publishing failed. Files in that directory belong to an earlier run.
- **`the previous run's module rows` or `per-instance rows could not be withdrawn`.** The run stops so stale rows are not left over new results. Fix the write error and rerun.
- **Power run warns that the halves cannot pair.** The netlist hashes differ or are missing. Rerun the synthesis and the power run against the same netlist.
