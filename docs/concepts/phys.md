---
description: Query saved synthesis and power artefacts by module and instance with rb phys, from the physical model each run writes.
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
- `module` reports one module's cells and area, then the instances of it and the power they burn. The name may be a design module from the synthesis half or a Liberty cell from the power half.
- `instance` reports one instance's power. A path that names a subtree instead of a leaf lists the leaves under it and rolls them up.

`--phys-dir` selects an artefact directory and `--manifest` names the document directly. An explicit `--manifest` wins over `--phys-dir`, which wins over discovery.

An unknown module or instance exits 2 and reports close candidates. A project with no manifest exits 2 naming `rb synth` and `rb power`.

## Read a model with only one half

A synthesis fills the model's `modules` half and a power run fills its `instances` half. A run of both into the same artefact directory, for the same top, produces a complete model in either order; see [Synthesis](synthesis.md#inspect-artefacts) and [Power Analysis](power.md#inspect-artefacts).

With one half absent, every verb still answers from the half that is present and says which command produces the other. `rb phys instance` is the exception: instance rows exist only in the power half, so it exits 2 pointing at `rb power`.

## Roll up a hierarchy

The model records leaf values only, because a subtree sum depends on the hierarchy the consumer projects onto. `rb phys instance <path>` is that consumer: it sums the leaves under the path at query time and leaves the document unchanged.

The rollup adds the four power columns directly. Area is joined in through each leaf's module, so it covers only the leaves whose module has a synthesis row; the reported `modules_matched` count says how many that was.

## Machine payloads

`--machine` emits the payload the verb built, carrying its own `schema_version`, the project-relative manifest and model paths, the run header, the artefact block, and the verb's data: rankings for `summary`, the module row plus its instances for `module`, and the row or subtree plus its rollup for `instance`.

Each payload also carries `halves` and `missing_halves`, which report which halves the model has and which command fills each one. A `null` value means the run did not measure it; `0` means it measured zero.
