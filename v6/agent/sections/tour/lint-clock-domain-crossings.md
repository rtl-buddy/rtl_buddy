## Lint clock-domain crossings

`rb cdc` runs [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc) with the design's SDC and an optional waiver file, and reports every crossing it traced.

```bash
uv run rb cdc-regression -c lint/cdc/cdc_regression.yaml
```

```text
                                     CDC Regression Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ CDC Analysis              ┃ Result ┃ Description                     ┃ Violations ┃ Suppressed ┃ Crossings ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ ip_cdc_handshake_lint     │ PASS   │ no rule violations              │ 0          │ 0          │ 3         │
│ demo_tiny_alu_subsys_lint │ PASS   │ no rule violations (2           │ 0          │ 2          │ 10        │
│                           │        │ suppressed)                     │            │            │           │
│ demo_cdc_src_sync_lint    │ PASS   │ no rule violations              │ 0          │ 0          │ 4         │
│ demo_cdc_open_lint        │ PASS   │ no rule violations              │ 0          │ 0          │ 5         │
│ demo_cdc_mem_macro_lint   │ PASS   │ no rule violations              │ 0          │ 0          │ 3         │
└───────────────────────────┴────────┴─────────────────────────────────┴────────────┴────────────┴───────────┘
                                          Reg Level: 0
                                   Results: 5 PASS (5 total)
```

The subsystem's ten crossings are the two handshakes, the async FIFO, and the two level synchronizers in the diagram above. Each crossing is also written to `cdc.json` with its source and destination flop, width, and synchronizer depth, so the two suppressed findings are auditable rather than silent. See the [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc) rule set and the FPGA page's [CDC constraint check](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpga/) for the flow around it.
