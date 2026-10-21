## `--run-tag` covers the test flows, not every reader

`--run-tag` is accepted by `rb test`, `rb randtest`, `rb regression`, the dispatch job commands and `rb graph results`. Other commands do not see tagged runs:

- `rb wave`, `rb cov`, `rb phys` and other flow commands read the flat `artefacts/<test>/`. Open a tagged trace by path: `artefacts/.runs/<tag>/<test>/dump.fst`.
- The hub, the MCP server and `rb graph query` read the untagged `artefacts/graph/results-overlay.json`. Publish a tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.
- A relative path in `compile-time` builder opts resolves one directory deeper under a tag. Write `${RTL_BUDDY_PROJECT_ROOT}/...`.
- Merged coverage is not namespaced: `--coverage-merge*` writes `<command_root>/cov_dir/` whatever the tag, so merge in only one concurrent run.
