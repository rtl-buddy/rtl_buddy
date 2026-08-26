## Results table

`rb synth` prints a results table after each run. Columns appear conditionally:

```
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Synthesis        ┃ Result ┃ Description      ┃ Gates ┃ Area       ┃ WNS       ┃ TNS       ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ sandbox_synth    │ PASS   │ Synthesis passed │ 18    │ -          │ -         │ -         │
│ sandbox_sky130   │ PASS   │ Synthesis passed │ 18    │ 178.92 µm² │ +8.882 ns │ -         │
│ sandbox_openroad │ PASS   │ Synthesis passed │ 18    │ 179.00 µm² │ +6.754 ns │ +0.000 ns │
└──────────────────┴────────┴──────────────────┴───────┴────────────┴───────────┴───────────┘
```

| Column | Source | When shown |
|--------|--------|-----------|
| **Gates** | Yosys `stat` cell count | All lib-mapped flows |
| **Area** | Yosys `stat -liberty` / OpenROAD `report_design_area` | Lib-mapped flows |
| **WNS** | Yosys: clock period − critical path delay; OpenROAD: `report_checks -path_delay max` | Lib-mapped flows with SDC |
| **TNS** | OpenROAD `report_tns` — sum of all negative endpoint slacks | OpenROAD backend with SDC |

WNS and TNS are positive when timing is met and negative when violated. TNS = 0 means no violations; a negative TNS indicates the total repair budget needed.

**WNS difference between backends:** Yosys computes WNS as `period − critical_path`, which always reports positive slack. OpenROAD's `report_checks` reports the actual worst slack across all timing paths. The two values are closely aligned for single-clock designs.
