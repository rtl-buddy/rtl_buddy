---
description: Query saved synthesis and power artefacts by module and instance with rb phys or the hub's synth+power pane, from the physical model each run writes.
---

# Physical Metrics

`rb phys` reads the physical model `rb synth` and `rb power` already write and answers per-module and per-instance questions from it. It runs no tools and writes nothing.

## Read the model a run produced

Every synthesis and power run writes `phys-model.json` and `phys-manifest.json` into an artefact directory — its own, or the one a power run's `phys-run:` names. Without an override, `rb phys` reads the newest `phys-manifest.json` under the project root.

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

## List the runs a project holds

A project accumulates one artefact directory per partition, per corner and per power mode. `rb phys runs` lists them, newest first:

```bash
rb phys runs
rb phys runs --limit 0
```

Each row carries the run name and top, the backends that produced it, the power mode and what drove the switching, a fingerprint of the configuration that shaped the netlist, the `rb xplr` experiment id when the run sits under one, and when it was generated. The row marked `*` is the newest — the run the other verbs read without `--phys-dir`. The artefact directories are printed under the table rather than in it, one per run, because that is the value the next command takes:

```bash
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

The listing reads one manifest per run and opens no model, so it stays cheap on a project with many. A manifest that cannot be read is listed with its error rather than dropped: discovery found the file, and a menu that omitted it would report fewer runs than the project has. A project with no physical artefacts lists nothing and exits 0, unlike the other three verbs — they were asked about a run, and this one is asking what runs there are.

## Tell two runs apart

Both producers record what *shaped* a run beside what it measured, so two runs of one design are two measurements rather than one document read twice.

`rb power` records its **mode** — `static` or `dynamic` — and a compact **activity** block: the resolved source (`default`, `synthetic`, `saif` or `vcd`), the trace and its sha256, the test whose artefact directory the trace came out of, the scope, and the toggle and duty a synthetic run used. Without them a µW figure carries no statement of what it measures: leakage plus internal and a SAIF-driven total print in the same column. Each detail belongs to the source that consumed it: a `saif` run records no toggle and duty, because a trace is what drove it, and a `default` or `synthetic` run records no trace, hash, test or scope even when its config still carries `activity.saif` and `activity.scope`, because the analysis read neither. The test name is derived from the layout — `verif/demo/artefacts/csr_smoke/dump.saif` was produced by `csr_smoke` — and is absent for a trace kept anywhere else. The sha256 identifies the trace by its bytes: a `dump.saif` is rewritten in place by the next run of the test behind it, so a re-captured trace is a different measurement under an unchanged path. It is part of the run's identity and not of its label — a label is a table cell — so two runs against two versions of one trace read alike in the `activity` column and apart in the block behind it.

Both flows record a **config fingerprint**: the platform, the effort, the constraints file and its sha256, and a short digest of the effective tool options. The digest covers what the backend's generated script actually consumes, and nothing else, so a field the script never reads is left out rather than reporting a difference the netlist cannot have. A **Yosys synthesis** feeds the elaboration settings (frontend, the two correctness-gate modes, and under `slang` the plugin path and `single-unit`), the resolved `synth-args`, the parameters and defines, and the branch it took: a mapped run adds the ABC delay target read out of the SDC and the resolved Liberty list, and drops `abc-args`, which its hard-coded ABC script ignores, while an unmapped run does the reverse. An **OpenROAD synthesis** feeds the same elaboration settings with `synth-args` taken from the effort — that is where its stage-1 script takes it from — the resolved Liberty list, which both its stages read, and from stage 2 the command `strategy` maps to, the sha256 of the effort's `pre-sta-tcl`, and the resolved LEF list. Both library lists are the resolved ones rather than the platform name, because a config's own `lib-paths` and `lef-paths` are appended to the platform's and are the whole of the list when there is no platform. A **power run** feeds the tool, netlist source, the identity of the upstream run it read, the resolved technology its script reads, mode, activity source and register level; its `tool_overrides` is left out because no power backend reads it. The technology is the Liberty and the one or two LEFs the generated Tcl names, in script order, for the reason the synthesis library lists are resolved rather than named by platform: a `cfg-pnr-platforms` entry repointed at another corner is the same name. The upstream identity is what `netlist_source` alone does not say — which synthesis or which place-and-route, not merely which kind: a `netlist-source: synth` run records the sha256 of the netlist it measured and a `netlist-source: pnr` run the project-relative path of the routed database it read. The constraints are the SDC the run actually read: a `netlist-source: pnr` run with no explicit `constraints:` records the router's `<top>.routed.sdc` it was given. The constraints sha256, and the activity trace's, are taken before the tool is launched and re-checked when it returns, because a digest computed at publication would identify a file replaced while the run worked — a routed SDC a concurrent `rb pnr` rewrote, a trace the next run of its test re-captured — and record it as what the run measured. A file that moved underneath the run records `null` and logs a warning. It is taken over the *effective values*, rendered as canonical JSON, not over the files they came from, so two configs that spell one setting differently and resolve to the same options are one experiment. As a summary it reads `nangate45 · timing-opt · sdc a1b2c3d4 · opts 5655beea1f20`.

A manifest under `artefacts/xplr/<exp-id>/` carries that **experiment id** in every payload's run header and in the run listing. The id comes from the path, so it is there before the experiment's `record.json` exists; the record is read only for the experiment's `hypothesis`, which becomes the entry's label. A malformed record costs the label and nothing else. A run inside a materialized checkout is labelled too: `rb xplr materialize` puts the worktree at `artefacts/xplr/worktrees/<exp-id>/`, so the id is one level further down and the reproducible runs — the ones checked out at a pinned sha — are exactly the ones that must not lose it. The `worktrees/` directory is not itself an experiment, and a worktree sidecar naming another location says this checkout is not that experiment's.

None of this gates the merge. The netlist sha256 both producers record is what decides whether two halves describe one design, and it is the stronger test in both directions: the same options can produce two netlists, and two option sets can produce one. That gate is also what keeps same-top experiments from cross-pollinating a merged model — a power half only pairs with the synthesis whose bytes it measured. A power run's `phys-run:` chooses which directory the two halves are asked to meet in and changes nothing about the gate; when the gate then refuses, that run says so rather than publishing a half-filled model quietly.

The keys are additive. A document written by an earlier rtl-buddy carries none of them, and every reader reports them as not recorded rather than refusing the document.

## Read a model with only one half

A synthesis fills the model's `modules` half and a power run fills its `instances` half. The two meet in one artefact directory, for the same top, and produce a complete model in either order. Which directory that is is the power run's to say: by default each run publishes into its own, so the halves meet only where a power run is named after the synthesis it reads and configured in the same directory, and `phys-run: <synth run>` in `power.yaml` names the synthesis run to publish beside instead. See [Pair the model with a synthesis run](power.md#pair-the-model-with-a-synthesis-run), [Synthesis](synthesis.md#inspect-artefacts) and [Power Analysis](power.md#inspect-artefacts).

With one half absent, every verb still answers from the half that is present and says which command produces the other — either because the other command has not run yet, because it published somewhere else, or because it could not merge with what is already here. The merge is gated on the netlist hash both producers record, so a power half taken from a routed database (`netlist-source: pnr`, which has no netlist to hash) cannot be paired with: a later `rb synth` into that directory would *replace* the model rather than complete it. The note says so, and names what does work — synthesise, then re-run `rb power` on the netlist the synthesis wrote, so both halves measure the same one. `rb phys instance` is the exception: instance rows exist only in the power half, so it exits 2 pointing at `rb power`.

## What the module join can answer

The two halves spell `module` in two namespaces. In the synthesis half it is an RTL module name, as Yosys' `stat` saw it. In the power half it is the Liberty cell each leaf instance is an instance of — `DFF_X1`, `NAND2_X1` — because a mapped netlist's leaves are cells, not RTL modules.

So `rb phys module` answers Liberty-cell questions: `rb phys module DFF_X1` reports every flop instance and the power they burn together.

It does not attribute power to an RTL module, and flattening the design does not change that. The join matches the power half's `module` field as it stands, and that field holds the cell a leaf is an instance of — so no leaf row carries `u_cpu`'s name, or the top's, and the join finds nothing. When a name resolves out of the synthesis half alone and the power half is populated, the payload's `instance_join` says so in words and the console prints it, so an empty instance list is never mistaken for "this block burns nothing". Ask what a block burns by its instance path instead: `rb phys instance u_cpu` sums the leaf rows under it — see [Roll up a hierarchy](#roll-up-a-hierarchy).

Nothing stops one name from being in both namespaces — a Liberty cell named after a block, or an RTL module called `DFF_X1`. Then the two halves are measuring two different things under one word, and `rb phys module` says so: `namespaces` lists both and `instance_join` carries a collision note the console prints. The row and the instances are still reported, and still not added together; the note is what keeps a module's cells and area beside a cell type's power from reading as one block's totals.

See [Known Issues](../known-issues.md#rb-phys-module-reports-no-power-for-an-rtl-module).

## Roll up a hierarchy

The model records leaf values only, because a subtree sum depends on the hierarchy the consumer projects onto. `rb phys instance <path>` is that consumer: it sums the leaves under the path at query time and leaves the document unchanged.

A path that names a row exactly is answered by that row. `match` reports which question was answered — `exact` or `prefix` — and the rollup answers that question and no other: the named row for an exact match, every leaf under the path for a prefix one. A path that is both a row and a prefix of other rows therefore rolls up to the row alone; the rows below it are still listed and still counted by `child_count`, and the console says they are there for navigation rather than in the total.

The rollup adds the four power columns directly, and reports nothing else. It carries no area: the model has no per-cell area to sum, and the only substitute available — joining each leaf's module to the synthesis half — would add the whole module's area once per leaf, on a name that may belong to the other namespace entirely. Area is reported per RTL module, by `rb phys module`, against the name Yosys counted it for.

## Browse the model in the hub

The hub serves the same model as a page. Start the browser layer and open `/phy`:

```bash
rb hub start --serve-viewer
```

`GET /phy.json` is the `rb phys summary` payload with no row limit, so the pane and the CLI cannot disagree about a number. It also carries the `rb phys runs` listing, which the pane renders as a run dropdown in its header: entries read `run · top · backends · mode (activity) · experiment`, mark the run being shown and the newest, and carry the artefact directory and the config fingerprint on the hover. Choosing one re-fetches `GET /phy.json?dir=<phys dir>`; the newest stays the default for a pane nobody has touched, and choosing the newest entry is how you go back to following it rather than pinning the run that happens to be newest today. An inbound `phys-focus` applies to the run the pane is showing — the message names a target and a metric, never a run, so switching runs is a gesture in the pane rather than something another app can do to it. The pane ranks modules by cells or area and instances by leakage, dynamic or total power, tints each ranked column, and filters the instance table to one module when you click it.

The pane holds every row, but renders the instance table 500 at a time with a `show more` / `show all` control under it — a mapped design's power half runs to six figures of leaf instances, and a table that rebuilt all of them on every sort click would freeze the tab. Selecting a row that ranks below the window moves the window to it, keeping the row and its neighbours in the ranking on screen without lifting the bound; the control then says how many rows sit above and below the slice. Sorting, filtering, the tints and the totals are computed over the whole set regardless of what is on screen.

`dynamic` is internal plus switching, summed in the browser rather than stored: no producer writes that column. The totals header shows the flow's own scraped total beside the sum of the rows, and says when they disagree.

Point the pane at a target from anywhere:

```bash
rb hub send phys-focus module:sub --metric area
rb hub send phys-focus instance:u_sub/_64_
```

An inbound focus is always visible: when the search box would hide the row it selects, the pane takes the search off and says so in its status line, and leaves it alone when the target already matches it. An unprefixed target is read as an instance path. The pane resolves exact leaf rows, so an instance target has to name a row: a subtree path — the one `rb phys instance` answers with `match: prefix` — selects nothing, and one of its children is what to send instead. The hub replays the latest focus to the pane when it registers, so sending one before the tab is open works — as does a selection the schematic broadcast before the pane's model had loaded. Clicking a module in the pane broadcasts `graph_focus` and clicking an instance broadcasts `selection_changed`, which the schematic follows. The pane roots the path it sends at the design top, since that is the schematic's coordinate and a model row is always relative to the top; on the way back in it resolves a path against the rows themselves, reading it both as sent and with a leading top level removed, so a design whose top name is also an instance name still selects the right row. The tables keep whatever spelling the model recorded. That top is the physical model's own, and the pane cannot know which view the schematic is displaying: a `/sch` showing a testbench around the DUT, or another design entirely, is sent a path that names no instance there and selects nothing. An instance path only means something inside the hierarchy the model recorded it in, so pair the pane with a schematic of the same design; see [Known Issues](../known-issues.md#phys-pane-and-schematic-selections-cross-only-within-one-hierarchy). See [Hub](hub.md#synthpower-pane) for the routes and the peer contract.

Clicking a module filters the instance table to it. When the name is an RTL module and every leaf carries a Liberty cell name, nothing matches, and the pane says so rather than showing an empty table. When the name is in both namespaces the lens looks complete instead — a cell type's leaves listed under a module's cells and area — so the pane prints the collision note there too, saying which measurement is whose. Both are the calls `rb phys module` makes on its own payload; see [What the module join can answer](#what-the-module-join-can-answer).

The same model also paints the design-knowledge-graph pane. Open `/gph`, tick `heat`, and its module nodes are filled with this model's numbers: cells and area joined from the synthesis half by module name, and the power of every leaf instance inside each module, rolled up by instance path because the power half's `module` column is a Liberty cell. Its metric switcher and its run dropdown are this pane's, over the same `GET /phy.json?dir=<phys dir>`, so a run chosen in either is the same run. See [Physical Heat on the Graph](graph.md#physical-heat-on-the-graph).

## Machine payloads

`--machine` emits the payload the verb built, carrying its own `schema_version`, the project-relative manifest and model paths, the run header, the artefact block, and the verb's data: rankings for `summary`, the module row plus its instances for `module`, and the row or subtree plus its rollup for `instance`.

Every run header also carries the run's identity: `power_mode`, `power_activity` (the recorded block plus a derived `label`), `config` with a `synth` and a `power` fingerprint (each plus a derived `summary`), and `xplr` — `{"id", "label"}` or `null`. Each half's `halves` entry echoes `mode` and `activity`, `null` on the synthesis half where neither exists.

`rb phys runs --machine` emits its own payload: `count`, the applied `limit`, and `runs`, newest first. Each entry carries `phys_dir` and `manifest`, `run`, `top`, `run_command`, `generated_at`, `backends`, `mode` and `activity`, `config`, a derived `fingerprint`, `xplr`, `newest`, and `error` — non-null only for a manifest that could not be read, where the rest of the entry is null and `phys_dir` still selects the run. `phys_dir` is derived from where the manifest was found, not from the document, so it is always relative to the root the listing was taken under.

`--limit` truncates the machine payload as well as the table, so a payload is exactly the rows the flag asked for and `--limit 0` is how an agent asks for all of them. A truncated list says so: `summary` carries `limit` beside its two rankings, `module` carries `limit` and `instance_count`, and `instance` carries `limit` and `child_count` — the counts are how many rows there are, never how many are listed. The figures beside a truncated list are still whole-set figures: `module`'s `power` sums every instance of the module and a prefix `instance`'s `rollup` sums every leaf under the path. The builders themselves default to the complete list, so only a caller that asks for a head gets one.

Each payload also carries `halves` and `missing_halves`, which report which halves the model has and which command fills each one. Each half's entry also carries `netlist_hash`: whether that half's producer recorded the hash of the netlist it measured, which is what the merge is gated on and therefore what a surface needs before advising anyone to run the other command. A `null` value means the run did not measure it; `0` means it measured zero. The `module` payload also carries `namespaces` — `["rtl"]`, `["liberty"]`, or both — and `instance_join`: `null` when the instance list needs no qualification, a sentence naming the Liberty-cell namespace limit when the module matched nothing because of it, and a collision sentence when the name is in both namespaces.

A manifest or model whose JSON root is not an object — an empty file rebuilt by hand, a truncated write, a path pointed at the wrong file — is refused by name, as an unreadable one is, and so is one whose blocks are not the shapes the read dereferences them as: the producer blocks and the totals are objects, the manifest's `model` and `phys_dir` paths strings, the model's two halves arrays of objects. A `null` block is not malformed, since that is what a half-filled run writes. Under `--machine` either arrives as the usual error envelope rather than a traceback.

`rb mcp` exposes the same query builders as `phys_runs`, `phys_summary`, `phys_module`, and `phys_instance`. `phys_runs` is where the `phys_dir` the other three take comes from; it needs no arguments and takes the same `limit`. They read files directly, run no EDA tool, and do not require a running hub. `phys_dir` and `manifest` are the tool arguments for `--phys-dir` and `--manifest`; a relative path resolves against the project root. All three take the same `limit` the flag does, with the same default: the head of the ranking, and `0` for every row. The payload says which it gave you — the applied `limit` sits beside the untruncated `instance_count`/`child_count`, and the `power` and `rollup` sums cover every matching row either way. A `phys_focus` tool mirroring `rb hub send phys-focus` joins them when a live hub is discovered. See [The MCP server](graph.md#the-mcp-server).
