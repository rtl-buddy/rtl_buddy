## Prove properties

`rb fpv` drives SymbiYosys and reports more than pass or fail. Vacuity, cone-of-influence, and dead-assume checks tell you whether a passing proof actually constrained anything.

```bash
cd fpv/demo_abv/demo_abv_features
uv run rb fpv
```

```text
                                            FPV Results Summary
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ FPV Run         ┃ Result ┃ Description     ┃ Mode ┃ Depth ┃ Engines      ┃ Vacuity     ┃ COI         ┃ Assumes        ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ …features_safety│ PASS   │ property proved │ bmc  │ 32    │ smtbmc yices │ -           │ 89% (8/9)   │ 0 used, 1 dead │
│ …features_vacuit│ PASS   │ property proved │ bmc  │ 32    │ smtbmc yices │ 1/2 vacuous │ 94% (16/17) │ 0 used, 1 dead │
└─────────────────┴────────┴─────────────────┴──────┴───────┴──────────────┴─────────────┴─────────────┴────────────────┘
                                         Results: 2 PASS (2 total)
```

Both runs pass, and both have something to fix: one assumption is dead in each, and one of the two vacuity properties never had its antecedent reached. Those are the findings a bare `sby` run hides. A failed proof gets `rb wave-fpv` to open the counterexample trace in Surfer. See [Formal property verification](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpv/).
