## Install the renderer

`rb hier` runs the `rtl-buddy-view` executable, which the `rtl-buddy-sch` distribution provides:

```bash
uv tool install rtl-buddy-sch
rb tool-check --explain rtl-buddy-view
```

Use `--tool /absolute/path/to/rtl-buddy-view` to pin a development build. Optional dependencies:

- Graphviz `dot`, to convert DOT to SVG or PNG.
- `pyslang`, for `--frontend slang`.

See [Installation](https://rtl-buddy.github.io/rtl_buddy/dev/install/#external-tools-by-feature) for tool setup. If an older `rtl-buddy-view` package conflicts with `rtl-buddy-sch`, see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#the-viewer-distribution-and-executable-have-different-names).
