## Run one test

A test is one named entry in a suite's `tests.yaml`. RTL Buddy resolves the model filelist, compiles, simulates, and grades the log.

```bash
cd verif/demo_tiny_alu
uv run rb test basic
```

```text
Compiling basic
Running simulation basic
      Test Results Summary
┏━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Test  ┃ Result ┃ Description ┃
┡━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━┩
│ basic │ PASS   │ (nwrn=  0)  │
└───────┴────────┴─────────────┘
       Builder: verilator
   Results: 1 PASS (1 total)
```

Artefacts land beside `tests.yaml` under `artefacts/basic/`: the compile and simulation logs, the FST trace, the generated filelist, and a `result.json` that records the verdict, builder, and run token. See [Tests](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/tests/).
