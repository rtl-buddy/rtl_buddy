---
description: Query saved synthesis and power artefacts by module and instance with rb phys or the hub's synth+power pane, from the physical model each run writes.
---

# Physical Metrics

`rb phys` reads the physical model that `rb synth` and `rb power` write and answers per-module and per-instance questions from it. It runs no tools and writes nothing.

## Read the model a run produced

Every synthesis and power run writes `phys-model.json` and `phys-manifest.json` into an artefact directory: its own, or the one a power run's `phys-run:` names. Without an override, `rb phys` reads the newest `phys-manifest.json` under the project root.

```bash
rb phys summary
rb phys summary --limit 0
rb phys summary --limit 0 --instances-limit none
rb phys module sub
rb phys instance u_sub
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

- `summary` reports the run header, design totals, the heaviest modules by cell count and the hottest instances by total power. `--limit 0` shows every row. `--modules-limit` and `--instances-limit` override `--limit` for one ranking each, and accept `none` to drop a ranking. `--limit 0 --instances-limit none` gives the full module table without the leaf instance rows, which on a mapped design is a few hundred rows instead of a few hundred thousand.
- `module` reports one module's cells and area, then its instances and their power. The name may be an RTL module from the synthesis half or a Liberty cell from the power half; see [What the module join can answer](#what-the-module-join-can-answer).
- `instance` reports one instance's power. A path that names a subtree lists the leaves under it, hottest first, and rolls them up. Paths are compared level by level, so `u_sub.u_leaf` and `u_sub/u_leaf` name the same instance.

`--phys-dir` selects an artefact directory and `--manifest` names the document directly. `--manifest` wins over `--phys-dir`, which wins over discovery.

An unknown module or instance exits 2 and lists close candidates. A project with no manifest exits 2 and names `rb synth` and `rb power`.

## List the runs a project holds

A project accumulates one artefact directory per partition, corner and power mode. `rb phys runs` lists them, newest first:

```bash
rb phys runs
rb phys runs --limit 0
```

Each row shows the run name and top, the producing backends, the power mode and switching-activity source, a fingerprint of the configuration that shaped the netlist, the `rb xplr` experiment id when the run sits under one, and the generation time. The row marked `*` is the newest, which the other verbs read without `--phys-dir`. Artefact directories are printed under the table, one per run, because the next command takes them:

```bash
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

The listing reads one manifest per run and opens no model. A manifest that cannot be read is listed with its error rather than dropped. A project with no physical artefacts lists nothing and exits 0, unlike the other verbs, which exit 2.

## Tell two runs apart

Both producers record what shaped a run beside what it measured, so two runs of one design are two measurements rather than one document read twice. The keys are additive: a document from an older rtl-buddy lacks them, and readers report them as not recorded.

**Power mode and activity.** `rb power` records its mode (`static` or `dynamic`) and an activity block, so a µW figure states what it measures. The block holds:

- the source: `default`, `synthetic`, `saif` or `vcd`;
- for a trace, its path, sha256, the test whose artefact directory produced it, and the scope;
- for `synthetic`, the toggle and duty used.

Each detail belongs to the source that consumed it. A `saif` run records no toggle and duty. A `default` or `synthetic` run records no trace, hash, test or scope even if its config carries `activity.saif` and `activity.scope`. The test name is derived from the layout (`verif/demo/artefacts/csr_smoke/dump.saif` came from `csr_smoke`) and is absent for a trace kept elsewhere. The sha256 identifies the trace by its bytes, so a trace re-captured under the same path is a different measurement.

**Config fingerprint.** Both flows record the platform, effort, constraints file and its sha256, and a short digest of the effective tool options, for example `nangate45 · timing-opt · sdc a1b2c3d4 · opts 5655beea1f20`. See [What the config fingerprint covers](#what-the-config-fingerprint-covers).

## What the config fingerprint covers

The digest covers only what the backend's script consumes, taken over the resolved values. Two configs that spell one setting differently are the same experiment, and a field the script never reads cannot change the digest.

| Flow | The digest covers |
| --- | --- |
| Yosys synthesis | Frontend and the two correctness-gate modes; under `slang`, the plugin path, `single-unit` and `best-effort-hierarchy`; resolved `synth-args`; parameters and defines. A mapped run adds the ABC delay target from the SDC and the resolved Liberty list and omits `abc-args`; an unmapped run does the reverse. |
| OpenROAD synthesis | The same elaboration settings, `synth-args` taken from the effort, the resolved Liberty list, and for stage 2 the command `strategy` maps to, the sha256 of the effort's `pre-sta-tcl` and the resolved LEF list. |
| Power | Tool, netlist source, the identity of the upstream run it read, the resolved technology (Liberty and the one or two LEFs the script names, in order), mode, activity source and register level. `tool_overrides` is omitted because no power backend reads it. |

- Library lists are the resolved ones, not the platform name, because repointing a `cfg-pnr-platforms` entry at another corner keeps its name.
- The upstream identity of a `netlist-source: synth` power run is the sha256 of the netlist it measured; for `netlist-source: pnr` it is the project-relative path of the routed database.
- A `netlist-source: pnr` run with no `constraints:` records the router's `<top>.routed.sdc`.
- The constraints and trace sha256 are taken before the tool launches and re-checked when it returns. A file replaced meanwhile records `null` and logs a warning.

## Label runs by experiment

A manifest under `artefacts/xplr/<exp-id>/` has that experiment id in every payload's run header and in the run listing. The id comes from the path, so it exists before the experiment's `record.json` does. The record supplies only the `hypothesis`, used as the label, and a malformed record costs the label only. A run in a checkout made by `rb xplr materialize` (`artefacts/xplr/worktrees/<exp-id>/`) is labelled too.

## Read a model with only one half

A synthesis fills the model's `modules` half and a power run fills its `instances` half. The halves meet in one artefact directory, for the same top, in either order. By default each run publishes into its own directory. To pair them, set `phys-run: <synth run>` in `power.yaml` so the power run publishes beside that synthesis. See [Pair the model with a synthesis run](power.md#pair-the-model-with-a-synthesis-run), [Synthesis](synthesis.md#inspect-artefacts) and [Power Analysis](power.md#inspect-artefacts).

The merge is gated on the netlist sha256 that both producers record. It decides whether two halves describe one design, and it keeps same-top experiments from mixing. `phys-run:` only chooses where the halves meet. When the gate refuses, the power run says so and publishes its half alone.

With one half absent, every verb still answers from the half present and names the command that produces the other. The other half may be missing because that command has not run, published elsewhere, or could not merge.

- A power half from a routed database (`netlist-source: pnr`) has no netlist to hash and cannot be paired. A later `rb synth` into that directory would replace the model rather than complete it. The note says this and names what works: synthesise, then rerun `rb power` on the netlist the synthesis wrote.
- `rb phys instance` needs the power half, because instance rows exist only there. Without it, the verb exits 2 and points at `rb power`.

## What the module join can answer

The two halves use `module` in different namespaces. In the synthesis half it is an RTL module name, as Yosys' `stat` saw it. In the power half it is the Liberty cell each leaf instance is an instance of, such as `DFF_X1` or `NAND2_X1`, because a mapped netlist's leaves are cells.

`rb phys module` therefore answers Liberty-cell questions: `rb phys module DFF_X1` reports every flop instance and their combined power.

It cannot attribute power to an RTL module, even in a flattened design, because no leaf row carries `u_cpu`'s name. When a name resolves in the synthesis half alone and the power half is populated, the payload's `instance_join` says so and the console prints it. An empty list does not mean the block burns nothing. Use the block's instance path instead: `rb phys instance u_cpu` sums the leaf rows under it. See [Roll up a hierarchy](#roll-up-a-hierarchy) and [Known Issues](../known-issues.md#rb-phys-module-reports-no-power-for-an-rtl-module).

A name can exist in both namespaces, such as a Liberty cell named after a block or an RTL module called `DFF_X1`. `rb phys module` then lists both in `namespaces`, and `instance_join` carries a collision note that the console prints. The row and the instances are reported but not added together, so a module's cells and area are not mistaken for a cell type's power.

## Roll up a hierarchy

The model stores leaf values only, because a subtree sum depends on the hierarchy the consumer projects onto. `rb phys instance <path>` sums the leaves under the path at query time and leaves the document unchanged.

- `match` says which question was answered: `exact` for a path that names a row, `prefix` for a subtree. The rollup answers that question only.
- A path that is both a row and a prefix of other rows rolls up to the row alone. The rows below it are still listed and counted in `child_count`, and the console says they are there for navigation and not in the total.
- The rollup adds the four power columns and nothing else. It has no area, because the model has no per-cell area to sum. Area is per RTL module, from `rb phys module`.

## Browse the model in the hub

The hub serves the same model as a page. Start the browser layer and open `/phy`:

```bash
rb hub start --serve-viewer
```

`GET /phy.json` is the `rb phys summary` payload with no row limit, plus the `rb phys runs` listing, so the pane and the CLI agree on every number. `GET /phy.json?dir=<phys dir>` selects a run.

- **Run dropdown.** Entries read `run · top · backends · mode (activity) · experiment`, mark the run being shown and the newest, and show the artefact directory and config fingerprint on hover. The newest is the default. Choose the newest entry to return to following it.
- **Tables.** The pane ranks modules by cells or area and instances by leakage, dynamic or total power, tints each ranked column, and filters the instance table to a module when you click it. When the clicked name is an RTL module and every leaf carries a Liberty cell name, nothing matches and the pane says so. When the name is in both namespaces, the pane prints the collision note. Both come from the payload `rb phys module` builds.
- **Large tables.** The instance table renders 500 rows at a time with a `show more` / `show all` control, because a mapped design's power half has six figures of leaf rows. Selecting a row below the window moves the window to it. Sorting, filtering, tints and totals cover the whole set.
- **Totals.** `dynamic` is internal plus switching, summed in the browser because no producer writes it. The totals header shows the flow's own scraped total beside the sum of the rows and says when they disagree. The synthesis scrape is anchored to the top module, so a disagreement is a real difference between the two measurements.

See [Hub](hub.md#synthpower-pane) for the routes and the peer contract. To drive the pane from another app, see [Focus the pane from another app](#focus-the-pane-from-another-app).

## Focus the pane from another app

Point the pane at a target from anywhere:

```bash
rb hub send phys-focus module:sub --metric area
rb hub send phys-focus instance:u_sub/_64_
```

- An unprefixed target is read as an instance path.
- The pane resolves exact leaf rows, so an instance target must name a row. A subtree path, the kind `rb phys instance` answers with `match: prefix`, selects nothing; send one of its children.
- An inbound focus is always visible. If the search box would hide the selected row, the pane clears the search and says so in its status line.
- The message names a target and a metric, never a run, so it applies to the run the pane is showing. Switching runs is done in the pane.
- The hub replays the latest focus when the pane registers, so a focus sent before the tab opens still lands.
- Clicking a module broadcasts `graph_focus` and clicking an instance broadcasts `selection_changed`, which the schematic follows.

The pane roots the paths it sends at the design top, which is the schematic's coordinate. On the way in, it matches a path against its rows both as sent and with a leading top level removed, so a design whose top name is also an instance name still selects correctly.

The pane cannot see which view the schematic displays. A `/sch` showing a testbench around the DUT, or another design, is sent a path that names no instance there and selects nothing. Open the schematic on the design the model was built from, the synthesis `top:`, so the two follow each other. See [Known Issues](../known-issues.md#phys-pane-and-schematic-selections-cross-only-within-one-hierarchy).

## Paint the graph with physical heat

The design-knowledge-graph pane can show this model. Open `/gph`, tick `heat`, and its module nodes are filled with the model's numbers. See [Physical Heat on the Graph](graph.md#physical-heat-on-the-graph).

- Cells and area come from the synthesis half by module name, counted once per module definition.
- Power is the sum of every leaf row inside every instantiation of the module, joined by instance path because the power half's `module` column holds Liberty cells.
- The pane prints how many instantiations and rows went into each figure, since the two halves do not count over the same thing.
- Its metric switcher and run dropdown use the same `GET /phy.json?dir=<phys dir>`, so a run chosen in either pane is the same run, and a `phys-focus` turns the overlay on.

## Machine payloads

`--machine` emits the payload the verb built. Every payload carries its own `schema_version`, the project-relative manifest and model paths, the run header and the artefact block. The verb's data is rankings for `summary`, the module row plus its instances for `module`, and the row or subtree plus its rollup for `instance`.

- **Run identity.** The run header carries `power_mode`, `power_activity` (the recorded block plus a derived `label`), `config` with a `synth` and a `power` fingerprint (each plus a derived `summary`), and `xplr` (`{"id", "label"}` or `null`). Each half's `halves` entry echoes `mode` and `activity`, `null` on the synthesis half.
- **Halves.** `halves` and `missing_halves` report which halves the model has and which command fills each. Each half's entry carries `netlist_hash`, whether its producer recorded the netlist hash, which the merge needs. `null` means not measured; `0` means measured zero.
- **Module join.** The `module` payload adds `namespaces` (`["rtl"]`, `["liberty"]` or both) and `instance_join`: `null` when the instance list needs no qualification, a sentence naming the Liberty-cell limit when the module matched nothing because of it, or a collision sentence.

`rb phys runs --machine` emits `count`, the applied `limit`, and `runs`, newest first. Each entry has `phys_dir`, `manifest`, `run`, `top`, `run_command`, `generated_at`, `backends`, `mode`, `activity`, `config`, a derived `fingerprint`, `xplr`, `newest` and `error`. `error` is non-null only for a manifest that could not be read, and then the other fields are null. `phys_dir` derives from where the manifest was found, relative to the listing's root, and always selects the run.

## Limit machine output

`--limit` truncates the machine payload as well as the table, so a payload holds exactly the rows the flag asked for. `--limit 0` asks for all rows. The builders default to the complete list; only a caller that asks for a head gets one.

- **Per-ranking limits.** On `summary`, `--modules-limit` and `--instances-limit` do the same per ranking. The `limits` block reports what each ranking was headed at, a number or `"none"`, beside the `limit` both would have followed.
- **Suppressed rankings.** A ranking asked for as `none` is an empty list and is not sorted or serialised. `counts` still reports how many rows the model holds, so it is not mistaken for a missing half; a missing half has `counts` `null`.
- **Truncation markers.** `summary` carries `limit` beside its rankings, `module` carries `limit` and `instance_count`, and `instance` carries `limit` and `child_count`. The counts are how many rows exist, not how many are listed.
- **Whole-set figures.** `module`'s `power` sums every instance of the module and a prefix `instance`'s `rollup` sums every leaf under the path, however the list is truncated.

A manifest or model whose JSON root is not an object is refused by name, as is one whose blocks have the wrong shape: producer blocks and totals must be objects, the manifest's `model` and `phys_dir` strings, and the model's two halves arrays of objects. A `null` block is valid, since that is what a half-filled run writes. Under `--machine` the refusal arrives as the usual error envelope.

## Use phys from MCP

`rb mcp` exposes the same builders as `phys_runs`, `phys_summary`, `phys_module` and `phys_instance`. They read files directly, run no EDA tool, and need no running hub. See [The MCP server](graph.md#the-mcp-server).

- `phys_runs` takes no required arguments and takes `limit`. It is where the `phys_dir` the other tools take comes from.
- `phys_dir` and `manifest` are the tool arguments for `--phys-dir` and `--manifest`. A relative path resolves against the project root.
- All four take `limit` with the flag's default: the head of the ranking, and `0` for every row.
- `phys_summary` also takes `modules_limit` and `instances_limit`, an integer or the string `"none"`. Omitted, the ranking follows `limit`.
- A `phys_focus` tool mirroring `rb hub send phys-focus` joins them when a live hub is discovered.
