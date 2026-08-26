## Results table

A summary table prints after each run:

```
                              P&R Results Summary
┏━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┓
┃ P&R Run  ┃ Result ┃ Desc     ┃ Cells ┃ Area    ┃ WNS Setup┃ WNS Hold ┃ DRCs ┃
┡━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━┩
│ demo_…   │ PASS   │ P&R …    │ 1392  │ 3213 µm²│ +4.350 ns│ +0.080 ns│ 0    │
└──────────┴────────┴──────────┴───────┴─────────┴──────────┴──────────┴──────┘
```

- **Cells** — `Number of instances` from OpenROAD's floorplan log.
- **Area** — `Design area … um^2` from `report_design_area`.
- **WNS Setup / WNS Hold** — `report_worst_slack -max` / `-min`.
- **DRCs** — non-empty line count of `route.drc.rpt`. Zero == clean route.
