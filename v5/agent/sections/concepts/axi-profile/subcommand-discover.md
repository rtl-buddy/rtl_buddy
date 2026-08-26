## Subcommand: `discover`

```bash
# Generate axi-bundles.yaml at the path declared in models.yaml
rb axi-profile discover soc_top

# Custom output path
rb axi-profile discover soc_top -o /tmp/axi-bundles.yaml

# Different models.yaml
rb axi-profile discover soc_top -c design/soc_top/models.yaml
```

The runner writes a stripped, deduplicated filelist for the model, then invokes `axi-profiler discover --top <model> --filelist <fl> --output <path>`. When `-o` is omitted, the output goes to the model's `axi_bundles:` path if set, otherwise to `artefacts/axi/<model>/axi-bundles.yaml`.

The generated `axi-bundles.yaml` is a checked-in manifest — re-running `discover` after RTL changes lets you diff the manifest in code review. The `--amend` option is reserved for a future user-edit merge workflow; passing it today emits a warning.
