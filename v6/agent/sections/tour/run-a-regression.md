## Run a regression

A regression is a list of suites with a level per test. The default level runs the sanity tier; `-l 1000` adds the heavier demos.

```bash
uv run rb regression -c regression.yaml
```

```text
                                    Regression Results Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Suite                                 ┃ Test         ┃ Result ┃ Description                ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ verif/apb/tests.yaml                  │ smoke        │ PASS   │ (nwrn=  0)                 │
│ verif/ip_cdc_sync/tests.yaml          │ smoke        │ PASS   │ (nwrn=  0)                 │
│ verif/ip_cdc_handshake/tests.yaml     │ smoke        │ PASS   │ (nwrn=  0)                 │
│ verif/ip_async_fifo/tests.yaml        │ smoke        │ PASS   │ (nwrn=  0)                 │
│ verif/demo_tiny_alu/tests.yaml        │ basic        │ PASS   │ (nwrn=  0)                 │
│ verif/demo_tiny_alu/tests.yaml        │ ops_sweep    │ PASS   │ (nwrn=  0)                 │
│ verif/demo_tiny_alu/tests.yaml        │ flags        │ PASS   │ (nwrn=  0)                 │
│ verif/demo_tiny_alu/tests.yaml        │ random       │ PASS   │ (nwrn=  0)                 │
│ verif/demo_tiny_alu_cocotb/tests.yaml │ cocotb_random│ SKIP   │ lvl 1000 > cmd end_level 0 │
│ verif/demo_tiny_alu_sc/tests.yaml     │ basic_sc     │ PASS   │ ED (0 errors)              │
│ verif/demo_tiny_alu_subsys/tests.yaml │ csr_smoke    │ SKIP   │ lvl 1000 > cmd end_level 0 │
│ verif/demo_axi_2x2/tests.yaml         │ basic_traffic│ SKIP   │ lvl 1000 > cmd end_level 0 │
└───────────────────────────────────────┴──────────────┴────────┴────────────────────────────┘
                                       Builder: verilator
                                       Builder Mode: reg
```

Rows above are a subset; the full run lists every suite. The same manifest runs a SystemVerilog suite, a SystemC cosimulation, and (at level 1000) cocotb suites, with one result table. Add `--dispatch local-parallel` or a Slurm backend to fan the suites out. See [Regressions](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/regressions/) and [Parallel dispatch](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/).
