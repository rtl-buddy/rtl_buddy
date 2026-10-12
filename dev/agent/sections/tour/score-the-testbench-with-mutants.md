## Score the testbench with mutants

`rb mut` injects one bug at a time into a design file and asks whether the proof (or a simulation oracle) catches it. The score is the fraction caught.

```bash
cd fpv/demo_abv/demo_abv_basic
uv run rb mut run
```

```text
                                        Mutation Testing Results
┏━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Mutant            ┃ Operator    ┃ Outcome  ┃ Verdict  ┃ Predicted Signals ┃ Mutation                          ┃
┡━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ m0000_arith_flip  │ arith_flip  │ SURVIVED │ fpv=PASS │ -                 │ line 17: `+` -> `-`               │
│ m0001_cond_negate │ cond_negate │ KILLED   │ fpv=FAIL │ -                 │ line 32: negate if cond `en && …` │
│ m0002_cond_const  │ cond_const  │ KILLED   │ fpv=FAIL │ -                 │ line 30: force if cond `!rst_n` … │
│ m0003_assign_drop │ assign_drop │ KILLED   │ fpv=FAIL │ cnt               │ line 31: drop `cnt <= ...;`       │
│ m0007_assign_drop │ assign_drop │ SURVIVED │ fpv=PASS │ cnt               │ line 33: drop `cnt <= ...;`       │
│ m0011_cond_const  │ cond_const  │ SURVIVED │ fpv=PASS │ -                 │ line 32: force if cond … -> `0`   │
└───────────────────┴─────────────┴──────────┴──────────┴───────────────────┴───────────────────────────────────┘
                              Mutation score: 58.3% (killed 7 / scored 12)
                             Survived: 5   Errored: 0   Baseline: fpv=PASS
                    Predicted-observable misses (weak properties): m0007_assign_drop
```

Rows above are a subset of the twelve mutants. The last line is the actionable one: mutant `m0007` changes a signal the properties can observe and still survived, so the property set is weak there, not merely blind. The full matrix is also written as `mut_report.json`. See [Mutation testing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/mut/).
