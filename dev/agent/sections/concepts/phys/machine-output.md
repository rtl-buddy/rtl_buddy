## Machine output

`--machine` emits the payload the verb built, each with its own `schema_version`, the project-relative manifest and model paths, the run header and the artefact block.

- **Run identity.** The header carries `power_mode`, `power_activity`, `config` (`synth` and `power` fingerprints with a `summary`), and `xplr` (`{"id", "label"}` or `null`).
- **Halves.** `halves` and `missing_halves` report which halves the model has and which command fills each. `null` means not measured; `0` means measured zero.
- **Module join.** The `module` payload adds `namespaces` (`["rtl"]`, `["liberty"]` or both) and `instance_join`: `null`, or a sentence for the Liberty-cell limit or a name collision.
- **Runs.** `rb phys runs --machine` emits `count`, `limit` and `runs`, newest first. Each entry carries the listing's columns, `phys_dir` (pass it to `--phys-dir`), `manifest`, `newest` and `error`, which is set only for an unreadable manifest.

`--limit` truncates the machine payload as well as the table, and `--limit 0` asks for all rows. On `summary`, the `limits` block reports the limit each ranking used. A ranking set to `none` is an empty list, and `counts` still reports the rows the model holds (`null` means a missing half).

`module` carries `limit` and `instance_count`, and `instance` carries `limit` and `child_count`; these count rows that exist, not rows listed. `module`'s `power` and a prefix `instance`'s `rollup` always cover every row.
