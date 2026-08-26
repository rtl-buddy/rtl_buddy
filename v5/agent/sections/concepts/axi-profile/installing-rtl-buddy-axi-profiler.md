## Installing rtl-buddy-axi-profiler

```bash
uv tool install rtl-buddy-axi-profiler                    # base — discover, gen-monitor, run
uv pip install 'rtl-buddy-axi-profiler[parquet]'          # adds pyarrow for --emit-txns-parquet
uv pip install 'rtl-buddy-axi-profiler[notebook]'         # adds marimo + altair + polars for rb axi-profile notebook
```

The base install gives you `discover`, `gen-monitor`, and `run` without parquet. The `[parquet]` extra unlocks `--emit-txns-parquet`, which is the prerequisite for `rb axi-profile notebook`. The `[notebook]` extra additionally brings in marimo so the notebook can be launched.
