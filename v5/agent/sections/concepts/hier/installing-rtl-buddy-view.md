## Installing rtl-buddy-view

```bash
uv tool install rtl-buddy-view    # recommended — isolated tool env
# or
uv pip install rtl-buddy-view     # into the project venv
```

Once installed, the binary lands on `PATH` as `rtl-buddy-view`. Override with `--tool /absolute/path/to/rtl-buddy-view` if you need a specific build.

For `--format dot` rendering to SVG/PNG, install Graphviz (`brew install graphviz` / `apt install graphviz`) and pipe the output through `dot` (see [Output formats](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/hier/#output-formats) below).
