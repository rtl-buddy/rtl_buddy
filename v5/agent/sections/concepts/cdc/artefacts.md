## Artefacts

Per-analysis outputs land under `<suite>/artefacts/<analysis>/`:

| File | Contents |
|---|---|
| `cdc.f` | Generated filelist (unrolled, deduplicated) |
| `cdc.log` | Combined stdout/stderr from both analyzer invocations |
| `cdc.txt` | Human-readable findings report |
| `cdc.json` | Machine-readable JSON report (parsed for the results table) |

`rb cdc` runs the analyzer twice per analysis — once with `--format text` for human consumption, once with `--format json` for the parsed verdict. If this becomes a hotspot, both views could be rendered from a single JSON probe; today the duplicate elaborate keeps the output decoupled.
