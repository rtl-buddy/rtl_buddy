## Install the profiler

The base tool covers discovery, monitor generation and trace ingestion:

```bash
uv tool install rtl-buddy-axi-profiler
```

Add extras for optional outputs: `parquet` for transaction output, `notebook` for interactive analysis. Install one form, or the combined one for both:

```bash
uv tool install 'rtl-buddy-axi-profiler[parquet]'
uv tool install 'rtl-buddy-axi-profiler[notebook]'
uv tool install 'rtl-buddy-axi-profiler[parquet,notebook]'
```

Add `--force` to replace an existing tool environment. Pass `--tool /path/to/axi-profiler` to use a specific executable. See [Installation](https://rtl-buddy.github.io/rtl_buddy/dev/install/#external-tools-by-feature) for external-tool setup.
