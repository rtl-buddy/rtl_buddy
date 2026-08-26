## Find artefacts and logs

Outputs are under `artefacts/axi/`:

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

Files appear only for stages that produce them; a custom `-o` path replaces the corresponding default output.

Each subcommand returns the external profiler's exit code. For elaboration, ingest, or write failures, inspect the matching log. Configuration, missing manifest, missing trace, and missing notebook prerequisites are reported before invoking the tool.
