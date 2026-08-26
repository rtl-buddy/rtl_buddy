## Artefacts

Per-model and per-test outputs land under `artefacts/axi/`:

```
artefacts/axi/
├── <model>/
│   ├── axi.f                                    # filelist used by discover (gen-monitor reads only the manifest)
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

`axi-perf.json` is the artefact the hub's view-builder bakes into every generated `view.json` when `rb hub start --axi-perf-from <path>` is set — see [Hub](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/hub/#axi-perf-overlay-and-notebook-spawning) for the SPA overlay flow. (It is not consumed by a `rb hier` flag; `rb hier` has no `--overlay` option — the hub passes `--overlay axi-perf=<path>` to the renderer internally.)
