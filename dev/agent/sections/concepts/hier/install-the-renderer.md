## Install the renderer

`rb hier` shells out to the `rtl-buddy-view` executable. Install the current `rtl-buddy-sch` distribution:

```bash
uv tool install rtl-buddy-sch
rb tool-check --explain rtl-buddy-view
```

Use `--tool /absolute/path/to/rtl-buddy-view` to pin a development build. Optional dependencies are:

- Graphviz `dot` to convert DOT into SVG or PNG.
- `pyslang` when using `--frontend slang`.

See [Installation](https://rtl-buddy.github.io/rtl_buddy/dev/install/#external-tools-by-feature) for tool setup and [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#the-viewer-distribution-and-executable-have-different-names) if an older `rtl-buddy-view` package conflicts with `rtl-buddy-sch`.
