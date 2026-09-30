## Outputs

Each run writes below the directory containing its `models.yaml`:

```text
artefacts/elab/<model>/<base-or-profile>/
  elab.f
  elab.log
  result.json
```

- `elab.f` is the filelist with includes unrolled and path entries made absolute.
- `result.json` records the selected top, explicit and parsed source counts, error and warning counts, `max_parse_depth` (`null` when unset), elapsed time, peak worker memory, and the pyslang version.
- A missing `prepend_sources`, `append_sources`, or `include_dirs` entry gives a `FAIL` at stage `filelist` for that profile. The command continues with the remaining profiles.
- Machine mode returns the same result payload and writes JSONL events to `rtl_buddy.log`.
