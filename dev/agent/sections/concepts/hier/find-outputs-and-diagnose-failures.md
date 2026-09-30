## Find outputs and diagnose failures

Outputs are anchored to the primary config's directory, not the shell's current directory. A model render writes:

```text
<models.yaml directory>/artefacts/hier/<model>/
├── hier.f
└── hier.log
```

A testbench render writes to `artefacts/hier/<model>/tb/<testbench>/` under the `tests.yaml` directory. `hier.f` is the generated filelist and `hier.log` holds the renderer's stderr. Queries also write `query.log`.

`rb hier` exits with the renderer's exit code. For a parse, elaboration, or output failure, read `hier.log`. If the executable is not found, run `rb tool-check --explain rtl-buddy-view`.

For interactive browsing, `rb hub start --serve-viewer --model <name>` serves the same JSON hierarchy. See [Hub](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/hub/).
