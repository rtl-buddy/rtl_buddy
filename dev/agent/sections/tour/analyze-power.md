## Analyze power

`rb power` runs OpenROAD power analysis on either the synthesized or the routed netlist, with activity from a default, a synthetic toggle rate, or a SAIF captured from a real simulation. `rb saif` makes the SAIF from a test's FST.

```bash
(cd verif/demo_tiny_alu_subsys && uv run rb test csr_smoke)
uv run rb saif verif/demo_tiny_alu_subsys/artefacts/csr_smoke/dump.fst \
               verif/demo_tiny_alu_subsys/artefacts/csr_smoke/dump.saif
uv run rb power -c power/demo_tiny_alu_subsys/power.yaml -l 1000
```

```text
                                             Power Results Summary
┏━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Power Run     ┃ Result ┃ Mode    ┃ Source ┃ Activity  ┃ Parasitics ┃ Total    ┃ Internal ┃ Switching  ┃ Leakage    ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ …power_static │ PASS   │ static  │ synth  │ default   │ -          │ 1.800 mW │ 1.340 mW │ 316.000 µW │ 149.000 µW │
│ …power_dynamic│ PASS   │ dynamic │ synth  │ synthetic │ -          │ 1.480 mW │ 1.030 mW │ 279.000 µW │ 162.000 µW │
│ …power_saif   │ PASS   │ dynamic │ synth  │ saif      │ -          │ 2.590 mW │ 1.880 mW │ 562.000 µW │ 151.000 µW │
│ …power_postpnr│ PASS   │ dynamic │ pnr    │ saif      │ estimated  │ 2.810 mW │ 1.750 mW │ 906.000 µW │ 159.000 µW │
└───────────────┴────────┴─────────┴────────┴───────────┴────────────┴──────────┴──────────┴────────────┴────────────┘
                                          Results: 4 PASS (4 total)
```

Four activity sources, one table. The jump in switching power from the synthetic run to the SAIF run is the real traffic from `csr_smoke`; the further jump after P&R is the clock tree and estimated wire parasitics. See [Power Analysis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/).
