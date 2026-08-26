## Results table

A summary table prints after each run:

```
                                  FPV Results Summary
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ FPV Run         ┃ Result ┃ Description               ┃ Mode ┃ Depth ┃ Engines      ┃ Runtime ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ demo_fpv_fifo   │ PASS   │ property proved (bmc, 32) │ bmc  │ 32    │ smtbmc yices │ 0.4s    │
└─────────────────┴────────┴───────────────────────────┴──────┴───────┴──────────────┴─────────┘
```

- **Mode / Depth / Engines** — what was actually run, surfaced for quick triage.
- **Engine Results** — per-engine verdict mix parsed from `<workdir>/logfile.txt`'s `summary: engine_<N> (...) returned <verdict>` lines. Renders compactly: `1/1 pass (smtbmc yices)` for single-engine runs, `2/2 pass` when all of multiple engines agree, `1/2 pass (smtbmc yices won)` when only some succeed. Shows `-` when sby died before producing a logfile.
- **Runtime** — wall-clock seconds for the sby invocation.
- **Description** — `property proved (...)` on pass; on fail, the path to the counterexample VCD when available.

> **Per-property granularity is not surfaced.** Sby has no structured per-assertion output today — `fpv.xml` is JUnit-style per-task only and the prose log doesn't tag individual assertions. Per-engine status is the finest grain `rb fpv` exposes; per-property aggregation would need a yosys-frontend change to dump assertion identifiers + per-COI status.
