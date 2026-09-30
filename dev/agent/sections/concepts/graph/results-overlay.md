## Results Overlay

`rb graph results` writes `artefacts/graph/results-overlay.json` and never modifies `graph.json`. Add `--strict` to fail on the mismatches listed below.

Each entry is keyed by `test:<suite dir>#<test name>` and holds the latest status (from the test's `result.json`), seed, timestamp, compile duration and existing artefact paths. A test directory with artefacts but no result is `UNKNOWN`. Random-test iterations are listed under `runs`, and the newest gives the top-level status.

The refresh reports three mismatches against `graph.json`:

- `missing`: graph test nodes with no result.
- `unmatched`: results with no declared test node, such as generated sweep names.
- `problems`: unreadable result data.

To convert one regression's results, pass the tag it ran with:

- `--run-tag <name>` reads `<suite>/artefacts/.runs/<tag>/` and writes `artefacts/.runs/<tag>/graph/results-overlay.json`, so concurrent regressions do not collide.
- The hub, the MCP server and `rb graph query` read the untagged overlay. Publish a tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.
