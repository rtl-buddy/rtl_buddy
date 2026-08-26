## axi-profile notebook

```text
Usage: rtl-buddy axi-profile notebook [OPTIONS] TEST_NAME                              
                                                                                        
 launch the packaged marimo notebook against a test's per-txn parquet                   
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT  test from tests.yaml [required]                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config  -c              TEXT     tests.yaml to use [default: tests.yaml]      │
│ --port                         INTEGER  TCP port for marimo's edit server (default:  │
│                                         OS-assigned)                                 │
│ --foreground       --daemon             Run marimo in the foreground (default).      │
│                                         --daemon is accepted but currently falls     │
│                                         back to foreground; background detach is a   │
│                                         follow-up.                                   │
│                                         [default: foreground]                        │
│ --headless                              Forward `--headless --no-token` to marimo.   │
│                                         Used by the hub-initiated 'Open in marimo'   │
│                                         flow (Phase 2 of the marimo umbrella) — the  │
│                                         SPA opens the URL itself, so marimo          │
│                                         shouldn't auto-pop a browser and the auth    │
│                                         token is disabled for the loopback-only      │
│                                         handoff.                                     │
│ --marimo                       TEXT     path to the marimo binary (default: 'marimo' │
│                                         on PATH)                                     │
│                                         [default: marimo]                            │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
