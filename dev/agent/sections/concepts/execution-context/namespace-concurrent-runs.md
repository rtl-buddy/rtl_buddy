## Namespace concurrent runs

`rb test`, `rb randtest`, `rb regression`, and `rb graph results` accept `--run-tag <name>`. The tag moves that invocation's whole artefact tree under `artefacts/.runs/<tag>/`:

| Path | Without `--run-tag` | With `--run-tag sim-a` |
| --- | --- | --- |
| Per-test artefacts | `<suite>/artefacts/<test>[/run-NNNN]` | `<suite>/artefacts/.runs/sim-a/<test>[/run-NNNN]` |
| Result envelope | `<suite>/artefacts/<test>/result.json` | `<suite>/artefacts/.runs/sim-a/<test>/result.json` |
| Tree lock | `<suite>/artefacts/.rtl-buddy.lock` | `<suite>/artefacts/.runs/sim-a/.rtl-buddy.lock` |
| Command log | `<command_root>/rtl_buddy.log` | `<suite>/artefacts/.runs/sim-a/rtl_buddy.log` |
| Dispatch head outputs | `<suite>/artefacts/.dispatch/` | `<suite>/artefacts/.runs/sim-a/.dispatch/` |
| Results overlay | `<root>/artefacts/graph/results-overlay.json` | `<root>/artefacts/.runs/sim-a/graph/results-overlay.json` |
| Shared builds | `<suite>/artefacts/.shared-builds/` | unchanged — shared across tags |
| `graph.json` | `<root>/artefacts/graph/graph.json` | unchanged — the design graph is not per-run |

Run two tiers at once, one per simulator, and convert each run's results separately:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
rb graph results --run-tag verilator
rb graph results --run-tag icarus
```

A tag must be a single safe path segment: letters, digits, `.`, `_`, and `-`, at most 64 characters. `.`, `..`, and anything containing a path separator are rejected before any directory is created. The directory is dot-prefixed (`.runs`) so a tag can never be confused with a test of the same name, and so every existing reader of the tree — the results-overlay scan, the `+incdir+` walk prune, the artefact clearers — keeps skipping it.

Shared builds stay shared on purpose. A build directory is keyed on the compile fingerprint, including the toolchain executable, so two simulators already get separate `obj_dir`s and two tags that compile the same thing reuse one build instead of paying for it twice.

The compile runs one directory deeper under a tag, so a relative path in a builder mode's `compile-time` opts names a different file with and without one. Spell a project file as `${RTL_BUDDY_PROJECT_ROOT}/design/waive.vlt` instead; see [Simulator builders](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#simulator-builders).

Without `--run-tag` nothing moves. The flat layout, the lock path, the log location, and the result envelope bytes are exactly what they were.
