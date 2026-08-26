## Subcommand: `notebook`

```bash
# Foreground (default) — opens marimo in your browser
rb axi-profile notebook my_test

# Pin the marimo edit-server port
rb axi-profile notebook my_test --port 2718

# Hub-initiated (SPA opens the URL; marimo runs headless without a token)
rb axi-profile notebook my_test --headless
```

The runner resolves three things up-front:

1. The per-test parquet at `artefacts/axi/<test>/axi-txns.parquet` — missing → clear error pointing at `rb axi-profile run <test> --emit-txns-parquet`.
2. The notebook template via `importlib.resources.files('rtl_buddy_axi_profiler.notebook') / 'template.py'`.
3. The `marimo` binary on `PATH` — missing → install hint for `rtl-buddy-axi-profiler[notebook]`.

It then spawns `marimo edit <template>` with `$AXI_TXNS_PARQUET` exported so the template's first cell loads the parquet. `--headless` adds `--no-token` so the SPA can navigate to the URL without threading a per-session token through the hub→browser handoff — loopback-only, so the security trade is fine.

`--daemon` is accepted but currently falls back to foreground; background detach is a follow-up (same pattern as `rb hub start`).
