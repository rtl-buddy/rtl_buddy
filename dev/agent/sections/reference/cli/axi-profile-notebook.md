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
│                                         --daemon is accepted for compatibility and   │
│                                         also runs in the foreground.                 │
│                                         [default: foreground]                        │
│ --headless                              Forward `--headless --no-token` to marimo    │
│                                         for hub launches. The hub opens the URL, and │
│                                         the handoff is loopback-only.                │
│ --marimo                       TEXT     path to the marimo binary (default: 'marimo' │
│                                         on PATH)                                     │
│                                         [default: marimo]                            │
│ --help                                  Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
