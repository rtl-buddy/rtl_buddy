## Find artefacts and logs

```text
artefacts/axi/
├── <model>/
│   ├── axi.f
│   ├── axi-bundles.yaml
│   ├── axi-profile-discover.log
│   └── axi-profile-gen-monitor.log
└── <test>/
    ├── axi.f
    ├── axi-perf.json
    ├── axi-txns.parquet
    ├── axi-profile-run.log
    └── axi-profile-notebook.log
```

Files exist only for stages that ran, and a custom `-o` path replaces the matching default.

Each subcommand returns the profiler's exit code. For elaboration, ingest or write failures, read the matching log. Configuration errors and missing manifest, trace or notebook prerequisites are reported before the tool is invoked.
