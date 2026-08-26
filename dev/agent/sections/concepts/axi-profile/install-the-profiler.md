## Install the profiler

Install the base tool for discovery, monitor generation, and trace ingestion:

```bash
uv tool install rtl-buddy-axi-profiler
```

Include optional outputs when installing the tool:

```bash
uv tool install 'rtl-buddy-axi-profiler[parquet]'
uv tool install 'rtl-buddy-axi-profiler[notebook]'
uv tool install 'rtl-buddy-axi-profiler[parquet,notebook]'
```

Choose one command: `parquet` provides transaction output, `notebook` provides interactive analysis, and the combined form enables both. Add `--force` when replacing an existing tool environment. Pass `--tool /path/to/axi-profiler` to use a specific executable. See [Installation](https://rtl-buddy.github.io/rtl_buddy/dev/install/#external-tools-by-feature) for external-tool setup.
