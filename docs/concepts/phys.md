---
description: Query saved synthesis and power artefacts by module and instance with rb phys or the hub's synth+power pane, from the physical model each run writes.
---

# Physical Metrics

`rb phys` reads the physical model `rb synth` and `rb power` already write and answers per-module and per-instance questions from it. It runs no tools and writes nothing.

## Read the model a run produced

Every synthesis and power run writes `phys-model.json` and `phys-manifest.json` into its artefact directory. Without an override, `rb phys` reads the newest `phys-manifest.json` under the project root.

```bash
rb phys summary
rb phys summary --limit 0
rb phys module sub
rb phys instance u_sub
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

- `summary` reports the run header, the design totals, the heaviest modules by cell count, and the hottest instances by total power. `--limit 0` shows every row.
- `module` reports one module's cells and area, then the instances of it and the power they burn. The name may be a design module from the synthesis half or a Liberty cell from the power half; see [What the module join can answer](#what-the-module-join-can-answer).
- `instance` reports one instance's power. A path that names a subtree instead of a leaf lists the leaves under it, hottest by total power first, and rolls them up. Paths are compared level by level, so `u_sub.u_leaf` and `u_sub/u_leaf` name the same instance whichever spelling the tool wrote.

`--phys-dir` selects an artefact directory and `--manifest` names the document directly. An explicit `--manifest` wins over `--phys-dir`, which wins over discovery.

An unknown module or instance exits 2 and reports close candidates. A project with no manifest exits 2 naming `rb synth` and `rb power`.

## Read a model with only one half

A synthesis fills the model's `modules` half and a power run fills its `instances` half. A run of both into the same artefact directory, for the same top, produces a complete model in either order; see [Synthesis](synthesis.md#inspect-artefacts) and [Power Analysis](power.md#inspect-artefacts).

With one half absent, every verb still answers from the half that is present and says which command produces the other — unless that command could not merge with what is already here. The merge is gated on the netlist hash both producers record, so a power half taken from a routed database (`netlist-source: pnr`, which has no netlist to hash) cannot be paired with: a later `rb synth` into that directory would *replace* the model rather than complete it. The note says so, and names what does work — synthesise, then re-run `rb power` on the netlist the synthesis wrote, so both halves measure the same one. `rb phys instance` is the exception: instance rows exist only in the power half, so it exits 2 pointing at `rb power`.

## What the module join can answer

The two halves spell `module` in two namespaces. In the synthesis half it is an RTL module name, as Yosys' `stat` saw it. In the power half it is the Liberty cell each leaf instance is an instance of — `DFF_X1`, `NAND2_X1` — because a mapped netlist's leaves are cells, not RTL modules.

So `rb phys module` answers Liberty-cell questions: `rb phys module DFF_X1` reports every flop instance and the power they burn together.

It does not attribute power to an RTL module, and flattening the design does not change that. The join matches the power half's `module` field as it stands, and that field holds the cell a leaf is an instance of — so no leaf row carries `u_cpu`'s name, or the top's, and the join finds nothing. When a name resolves out of the synthesis half alone and the power half is populated, the payload's `instance_join` says so in words and the console prints it, so an empty instance list is never mistaken for "this block burns nothing". Attribution through the real hierarchy is a later phase of the physical-metrics epic.

Nothing stops one name from being in both namespaces — a Liberty cell named after a block, or an RTL module called `DFF_X1`. Then the two halves are measuring two different things under one word, and `rb phys module` says so: `namespaces` lists both and `instance_join` carries a collision note the console prints. The row and the instances are still reported, and still not added together; the note is what keeps a module's cells and area beside a cell type's power from reading as one block's totals.

## Roll up a hierarchy

The model records leaf values only, because a subtree sum depends on the hierarchy the consumer projects onto. `rb phys instance <path>` is that consumer: it sums the leaves under the path at query time and leaves the document unchanged.

The rollup adds the four power columns directly, and reports nothing else. It carries no area: the model has no per-cell area to sum, and the only substitute available — joining each leaf's module to the synthesis half — would add the whole module's area once per leaf, on a name that may belong to the other namespace entirely. Attributing area to an instance or a subtree arrives with the hierarchy join, Phase 5 of the physical-metrics epic ([rtl-buddy/rtl_buddy#558](https://github.com/rtl-buddy/rtl_buddy/issues/558)).

## Browse the model in the hub

The hub serves the same model as a page. Start the browser layer and open `/phy`:

```bash
rb hub start --serve-viewer
```

`GET /phy.json` is the `rb phys summary` payload with no row limit, so the pane and the CLI cannot disagree about a number. The pane ranks modules by cells or area and instances by leakage, dynamic or total power, tints each ranked column, and filters the instance table to one module when you click it.

The pane holds every row, but renders the instance table 500 at a time with a `show more` / `show all` control under it — a mapped design's power half runs to six figures of leaf instances, and a table that rebuilt all of them on every sort click would freeze the tab. Selecting a row that ranks below the window moves the window to it, keeping the row and its neighbours in the ranking on screen without lifting the bound; the control then says how many rows sit above and below the slice. Sorting, filtering, the tints and the totals are computed over the whole set regardless of what is on screen.

`dynamic` is internal plus switching, summed in the browser rather than stored: no producer writes that column. The totals header shows the flow's own scraped total beside the sum of the rows, and says when they disagree.

Point the pane at a target from anywhere:

```bash
rb hub send phys-focus module:sub --metric area
rb hub send phys-focus instance:u_sub/_64_
```

An inbound focus is always visible: when the search box would hide the row it selects, the pane takes the search off and says so in its status line, and leaves it alone when the target already matches it. An unprefixed target is read as an instance path. The pane resolves exact leaf rows, so an instance target has to name a row: a subtree path — the one `rb phys instance` answers with `match: prefix` — selects nothing, and one of its children is what to send instead. The hub replays the latest focus to the pane when it registers, so sending one before the tab is open works — as does a selection the schematic broadcast before the pane's model had loaded. Clicking a module in the pane broadcasts `graph_focus` and clicking an instance broadcasts `selection_changed`, which the schematic follows. The pane roots the path it sends at the design top, since that is the schematic's coordinate and a model row is always relative to the top; on the way back in it resolves a path against the rows themselves, reading it both as sent and with a leading top level removed, so a design whose top name is also an instance name still selects the right row. The tables keep whatever spelling the model recorded. That top is the physical model's own, and the pane cannot know which view the schematic is displaying: a `/sch` showing a testbench around the DUT, or another design entirely, is sent a path that names no instance there and selects nothing. Addressing one instance across two hierarchies arrives with the hierarchy join, Phase 5 of the physical-metrics epic ([rtl-buddy/rtl_buddy#558](https://github.com/rtl-buddy/rtl_buddy/issues/558)). See [Hub](hub.md#synthpower-pane) for the routes and the peer contract.

Clicking a module filters the instance table to it. When the name is an RTL module and every leaf carries a Liberty cell name, nothing matches, and the pane says so rather than showing an empty table. When the name is in both namespaces the lens looks complete instead — a cell type's leaves listed under a module's cells and area — so the pane prints the collision note there too, saying which measurement is whose. Both are the calls `rb phys module` makes on its own payload; see [What the module join can answer](#what-the-module-join-can-answer).

## Machine payloads

`--machine` emits the payload the verb built, carrying its own `schema_version`, the project-relative manifest and model paths, the run header, the artefact block, and the verb's data: rankings for `summary`, the module row plus its instances for `module`, and the row or subtree plus its rollup for `instance`.

`--limit` truncates the machine payload as well as the table, so a payload is exactly the rows the flag asked for and `--limit 0` is how an agent asks for all of them. A truncated list says so: `summary` carries `limit` beside its two rankings, `module` carries `limit` and `instance_count`, and `instance` carries `limit` and `child_count` — the counts are how many rows there are, never how many are listed. The figures beside a truncated list are still whole-set figures: `module`'s `power` sums every instance of the module and `instance`'s `rollup` sums every leaf under the path. The builders themselves default to the complete list, so only a caller that asks for a head gets one.

Each payload also carries `halves` and `missing_halves`, which report which halves the model has and which command fills each one. Each half's entry also carries `netlist_hash`: whether that half's producer recorded the hash of the netlist it measured, which is what the merge is gated on and therefore what a surface needs before advising anyone to run the other command. A `null` value means the run did not measure it; `0` means it measured zero. The `module` payload also carries `namespaces` — `["rtl"]`, `["liberty"]`, or both — and `instance_join`: `null` when the instance list needs no qualification, a sentence naming the Liberty-cell namespace limit when the module matched nothing because of it, and a collision sentence when the name is in both namespaces.

A manifest or model whose JSON root is not an object — an empty file rebuilt by hand, a truncated write, a path pointed at the wrong file — is refused by name, as an unreadable one is, and so is one whose blocks are not the shapes the read dereferences them as: the producer blocks and the totals are objects, the manifest's `model` and `phys_dir` paths strings, the model's two halves arrays of objects. A `null` block is not malformed, since that is what a half-filled run writes. Under `--machine` either arrives as the usual error envelope rather than a traceback.

`rb mcp` exposes the same query builders as `phys_summary`, `phys_module`, and `phys_instance`. They read files directly, run no EDA tool, and do not require a running hub. `phys_dir` and `manifest` are the tool arguments for `--phys-dir` and `--manifest`; a relative path resolves against the project root. All three take the same `limit` the flag does, with the same default: the head of the ranking, and `0` for every row. The payload says which it gave you — the applied `limit` sits beside the untruncated `instance_count`/`child_count`, and the `power` and `rollup` sums cover every matching row either way. A `phys_focus` tool mirroring `rb hub send phys-focus` joins them when a live hub is discovered. See [The MCP server](graph.md#the-mcp-server).
