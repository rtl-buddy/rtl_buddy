## Artefacts

Per-model outputs land under the current directory's `artefacts/hier/<model>/`:

| File | Contents |
|---|---|
| `hier.f` | Stripped, deduplicated filelist passed to the renderer |
| `hier.log` | Renderer stderr (its stdout goes to your terminal when `-o` is not set) |

When `-o <path>` is supplied the renderer writes directly to that path; otherwise its stdout is the diagram source itself.
