## Profile a test trace

```bash
rb axi-profile run my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile run my_test --emit-txns-parquet-path /tmp/txns.parquet
rb axi-profile run my_test --tb-prefix my_custom_wrapper
```

The test resolves its model, manifest, testbench scope, and newest trace. The default outputs are:

- `artefacts/axi/<test>/axi-perf.json` for aggregate bundle throughput and latency.
- `artefacts/axi/<test>/axi-txns.parquet` when transaction output is enabled.

An explicit Parquet path enables transaction output automatically. Use `--tb-prefix` when the simulator wrapper renames the testbench scope; pass an empty value to disable prefix matching.

The newest supported trace in `<suite>/artefacts/<test>/` wins:

| Trace | Handling |
| --- | --- |
| `dump.fst` | Read directly. |
| `dump.vcd` | Read directly. |
| `vcdplus.vpd` | Convert with `vpd2vcd`; use `vcd2fst` when available. |

VPD conversion writes `vpd-convert.log` and caches `vcdplus.fst` beside the input. A cache newer than the VPD is reused. Without `vcd2fst`, the larger temporary VCD is retained and read directly. A VCS installation that produced VPD normally supplies `vpd2vcd`; GTKWave supplies `vcd2fst`.
