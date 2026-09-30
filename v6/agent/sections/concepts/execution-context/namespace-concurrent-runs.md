## Namespace concurrent runs

`rb test`, `rb randtest`, `rb regression` and `rb graph results` accept `--run-tag <name>`, which moves that invocation's whole artefact tree under `artefacts/.runs/<tag>/`. Use it to run the same suite twice at once, for example one run per simulator:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
rb graph results --run-tag verilator
rb graph results --run-tag icarus
```

| Path | Without `--run-tag` | With `--run-tag sim-a` |
| --- | --- | --- |
| Per-test artefacts | `<suite>/artefacts/<test>[/run-NNNN]` | `<suite>/artefacts/.runs/sim-a/<test>[/run-NNNN]` |
| Result envelope | `<suite>/artefacts/<test>/result.json` | `<suite>/artefacts/.runs/sim-a/<test>/result.json` |
| Tree lock | `<suite>/artefacts/.rtl-buddy.lock` | `<suite>/artefacts/.runs/sim-a/.rtl-buddy.lock` |
| Command log | `<command_root>/rtl_buddy.log` | `<suite>/artefacts/.runs/sim-a/rtl_buddy.log` |
| Dispatch head outputs | `<suite>/artefacts/.dispatch/` | `<suite>/artefacts/.runs/sim-a/.dispatch/` |
| Results overlay | `<root>/artefacts/graph/results-overlay.json` | `<root>/artefacts/.runs/sim-a/graph/results-overlay.json` |
| Shared builds | `<suite>/artefacts/.shared-builds/` | unchanged, shared across tags |
| `graph.json` | `<root>/artefacts/graph/graph.json` | unchanged, not per-run |

- A tag is one path segment of letters, digits, `.`, `_` and `-`, at most 64 characters. `.`, `..` and anything containing a path separator are rejected before any directory is created.
- Shared builds stay shared. The build key includes the toolchain executable, so two simulators get separate `obj_dir`s, and two tags compiling the same thing reuse one build.
- Under a tag the compile runs one directory deeper, so a relative path in a builder mode's `compile-time` opts names a different file. Write project files as `${RTL_BUDDY_PROJECT_ROOT}/design/waive.vlt`; see [Simulator builders](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#simulator-builders).
- Without `--run-tag` the layout is the flat one in the left column.
