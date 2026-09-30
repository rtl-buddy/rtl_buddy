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
- `module` reports one module's cells and area, then its instances and their power. The name may be an RTL module (synthesis half) or a Liberty cell (power half); see [What the module join can answer](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#what-the-module-join-can-answer).
- `instance` reports one instance's power. A path that names a subtree lists the leaves under it, hottest first, and rolls them up. `u_sub.u_leaf` and `u_sub/u_leaf` name the same instance.

`--phys-dir` selects an artefact directory and `--manifest` names the manifest directly. `--manifest` wins over `--phys-dir`, which wins over discovery.
