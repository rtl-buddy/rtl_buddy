## Measure coverage

The `cov` builder mode compiles with Verilator coverage; `rb cov` then reads the saved artefacts without rerunning anything.

```bash
cd verif/demo_tiny_alu
uv run rb -M cov test basic ops_sweep flags random
uv run rb cov summary
```

```text
                                    Coverage — Totals
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Scope        ┃ Line        ┃ Branch      ┃ Toggle        ┃ Expression  ┃ Cover        ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ run          │ 41/55 (75%) │ 16/38 (42%) │ 276/316 (87%) │ 10/11 (91%) │ 22/22 (100%) │
│ basic        │ 41/55 (75%) │ 16/38 (42%) │ 159/316 (50%) │ 6/11 (55%)  │ 16/22 (73%)  │
│ flags        │ 37/55 (67%) │ 16/38 (42%) │ 261/316 (83%) │ 8/11 (73%)  │ 16/22 (73%)  │
│ ops_sweep    │ 41/55 (75%) │ 16/38 (42%) │ 234/316 (74%) │ 7/11 (64%)  │ 15/22 (68%)  │
│ random       │ 41/55 (75%) │ 16/38 (42%) │ 276/316 (87%) │ 10/11 (91%) │ 22/22 (100%) │
└──────────────┴─────────────┴─────────────┴───────────────┴─────────────┴──────────────┘
                                  Coverage — Coldest Files
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Scope                                 ┃ Line         ┃ Branch     ┃ Toggle         ┃ Expression ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ common/lvm_core.sv                    │ 18/32 (56%)  │ 6/20 (30%) │ -              │ -          │
│ design/demo_tiny_alu/demo_tiny_alu.sv │ 10/10 (100%) │ 2/2 (100%) │ 122/122 (100%) │ 6/6 (100%) │
│ verif/demo_tiny_alu/tb_top.sv         │ 12/12 (100%) │ 8/16 (50%) │ 66/106 (62%)   │ 4/5 (80%)  │
└───────────────────────────────────────┴──────────────┴────────────┴────────────────┴────────────┘
```

The coldest file is the shared testbench macro library, not the design: the DUT itself is at 100% on every metric. `rb cov module demo_tiny_alu` narrows the same data to one module. A regression run with `--coverage-merge --coverage-html` produces the merged LCOV report. See [Coverage](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/coverage/).
