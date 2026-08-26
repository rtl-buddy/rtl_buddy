## Artefacts

Per-model outputs land under the model's command root — `<dir of models.yaml>/artefacts/hier/<model>/` (in `--view tb` mode, `<dir of tests.yaml>/artefacts/hier/<model>/tb/<tb_name>/`). The artefact tree is anchored on the primary config's directory, not your shell's cwd — see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/). For example, `rb hier demo_top -c design/demo_top/models.yaml` writes under `design/demo_top/artefacts/hier/demo_top/`:

| File | Contents |
|---|---|
| `hier.f` | Stripped, deduplicated filelist passed to the renderer |
| `hier.log` | Renderer stderr (its stdout goes to your terminal when `-o` is not set) |

When `-o <path>` is supplied the renderer writes directly to that path; otherwise its stdout is the diagram source itself.
