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
- `module` reports one module's cells and area, then the instances of it and the power they burn. The name may be a design module from the synthesis half or a Liberty cell from the power half; see [What the module join can answer](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#what-the-module-join-can-answer).
- `instance` reports one instance's power. A path that names a subtree instead of a leaf lists the leaves under it, hottest by total power first, and rolls them up. Paths are compared level by level, so `u_sub.u_leaf` and `u_sub/u_leaf` name the same instance whichever spelling the tool wrote.

`--phys-dir` selects an artefact directory and `--manifest` names the document directly. An explicit `--manifest` wins over `--phys-dir`, which wins over discovery.

An unknown module or instance exits 2 and reports close candidates. A project with no manifest exits 2 naming `rb synth` and `rb power`.
