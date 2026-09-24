## `--run-tag` covers the test flows, not every reader

`--run-tag` is accepted by `rb test`, `rb randtest`, `rb regression`, the dispatch job commands, and `rb graph results`. Consequences to plan around:

- `rb wave`, `rb cov`, `rb phys`, and the other flow commands resolve the flat `artefacts/<test>/`. Open a tagged run's trace by its path, `artefacts/.runs/<tag>/<test>/dump.fst`.
- The hub, the MCP server, and `rb graph query` read the untagged `artefacts/graph/results-overlay.json`. Publish one tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.
- Two tagged Slurm runs of one suite serialise their build jobs. The `--dependency=singleton` rendezvous is keyed on the suite directory, which owns the shared build tree; the simulation fan-outs still overlap.
- A relative path in `builder-opts.<mode>.compile-time` resolves against the compile's working directory, which is one level deeper under a tag. Spell project files with `${RTL_BUDDY_PROJECT_ROOT}/...` so they resolve the same either way.
- Merged coverage is not namespaced. `--coverage-merge*` writes `<command_root>/cov_dir/` whatever the tag, so two concurrent tagged runs that both merge would write one directory. Merge in one run only, or merge afterwards from each run's per-test `coverage.dat`.
