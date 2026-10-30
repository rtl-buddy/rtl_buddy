## Profile a test trace

```bash
rb axi-profile run my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile run my_test --emit-txns-parquet-path /tmp/txns.parquet
rb axi-profile run my_test --tb-prefix my_custom_wrapper
```

The test supplies its model, manifest, testbench scope and newest trace. Outputs:

- `artefacts/axi/<test>/axi-perf.json`: aggregate throughput and latency per bundle.
- `artefacts/axi/<test>/axi-txns.parquet`: per-transaction data, only when enabled. An explicit `--emit-txns-parquet-path` enables it.

Use `--tb-prefix` when the simulator wrapper renames the testbench scope; an empty value disables prefix matching.

The newest supported trace in `<suite>/artefacts/<test>/` is used:

| Trace | Handling |
| --- | --- |
| `dump.fst` | Read directly. |
| `dump.vcd` | Read directly. |
| `vcdplus.vpd` | Convert with `vpd2vcd`, then `vcd2fst` when available. |

VPD conversion logs to `vpd-convert.log` and caches `vcdplus.fst` beside the input; a cache newer than the VPD is reused. Without `vcd2fst`, the larger temporary VCD is kept and read directly. `vpd2vcd` comes with the VCS installation that produced the VPD, and `vcd2fst` with GTKWave.
