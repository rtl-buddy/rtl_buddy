## Find outputs and diagnose failures

Outputs are anchored to the primary configuration directory, not the shell's current directory. A model render writes:

```text
<models.yaml directory>/artefacts/hier/<model>/
├── hier.f
└── hier.log
```

TB view writes under the `tests.yaml` directory at `artefacts/hier/<model>/tb/<testbench>/`. `hier.f` is the generated filelist and `hier.log` captures renderer stderr. Query invocations also write `query.log`.

`rb hier` returns the renderer's exit code. For parse, elaboration, or output failures, inspect `hier.log`. If the executable cannot be found, run:

```bash
rb tool-check --explain rtl-buddy-view
```

For interactive browsing, `rb hub start --serve-viewer --model <name>` builds and serves the same JSON hierarchy. See [Hub](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/hub/).
