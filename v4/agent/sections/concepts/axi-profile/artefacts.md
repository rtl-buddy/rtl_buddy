## Artefacts

Per-model and per-test outputs land under `artefacts/axi/`:

```
artefacts/axi/
├── <model>/
│   ├── axi.f                                    # filelist used by discover / gen-monitor
│   ├── axi-bundles.yaml                         # only when -o defaults here
│   ├── axi-profile-discover.log                 # stderr from `axi-profiler discover`
│   └── axi-profile-gen-monitor.log              # stderr from `axi-profiler gen-monitor`
└── <test>/
    ├── axi.f                                    # filelist used by run
    ├── axi-perf.json                            # aggregate per-bundle throughput / latency
    ├── axi-txns.parquet                         # per-transaction rows (only with --emit-txns-parquet)
    ├── axi-profile-run.log                      # stderr from `axi-profiler run`
    └── axi-profile-notebook.log                 # stderr from marimo
```

`axi-perf.json` is the artefact picked up by `rb hier --overlay axi-perf=...` and by the hub's view-builder when `--axi-perf-from` is wired up — see [Hub](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/hub/) for the SPA overlay flow.
