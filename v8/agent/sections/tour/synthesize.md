## Synthesize

`rb synth` runs Yosys from a `synth.yaml` entry, generic or Liberty-mapped, and reports gate count, area, and worst slack when a library and SDC are present.

```bash
uv run rb synth-regression -c synth_regression.yaml
uv run rb synth demo_tiny_alu_subsys_synth_nangate45 -c synth/demo_tiny_alu_subsys/synth.yaml
```

```text
                           Synthesis Regression Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Synthesis                                ┃ Result ┃ Description                ┃ Gates ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ demo_tiny_alu_synth_generic              │ PASS   │ Synthesis passed           │ 287   │
│ demo_tiny_alu_subsys_synth_generic       │ PASS   │ Synthesis passed           │ 3847  │
│ demo_cdc_src_sync_synth_generic          │ PASS   │ Synthesis passed           │ 13    │
│ demo_synth_incdir_synth_generic          │ PASS   │ Synthesis passed           │ 42    │
│ demo_synth_macro_generic                 │ PASS   │ Synthesis passed           │ 16    │
└──────────────────────────────────────────┴────────┴────────────────────────────┴───────┘

                                  Synthesis Results Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Synthesis                            ┃ Result ┃ Description      ┃ Gates ┃ Area        ┃ WNS       ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ demo_tiny_alu_subsys_synth_nangate45 │ PASS   │ Synthesis passed │ 3879  │ 8345.48 µm² │ +5.363 ns │
└──────────────────────────────────────┴────────┴──────────────────┴───────┴─────────────┴───────────┘
```

The Nangate45 run is the input to the next two sections. Per-module cell counts and areas from every synthesis run are kept in a physical model that `rb phys` queries later without rerunning the tool. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/synthesis/) and [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/phys/).
