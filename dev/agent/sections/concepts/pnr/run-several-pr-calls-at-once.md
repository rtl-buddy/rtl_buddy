## Run several P&R calls at once

`rb pnr` locks the suite's whole `artefacts/` tree, so a second untagged call on the same `pnr.yaml` is refused, even for another run. Give each concurrent call its own `--run-tag`, for example to sweep one block's settings:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --run-tag util55 &
rb pnr alu_pnr -c pnr/alu/pnr.yaml --run-tag util60 &
wait
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --run-tag util60 --png
```

A tagged call writes each run under `artefacts/.runs/<tag>/<run>/`, with its own lock and `rtl_buddy.log` in `artefacts/.runs/<tag>/`. Tag rules are in [Namespace concurrent runs](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#namespace-concurrent-runs).

| Calls | Result |
| --- | --- |
| Two untagged, or two with the same tag | They share one lock; the second is refused. |
| Different tags | They run side by side. |
| One tagged, one untagged | Side by side, except while the tagged call copies an input from the untagged tree: it waits for that lock, logging `artifact_lock.wait` every five minutes. |
| A tagged call whose tree another call holds only to copy a block abstract out | It waits for the copy. |
| A whole-suite call pulling in a run from a suite whose tagged tree another call holds | Refused, naming that tree. |

- With no run name, the tag also covers runs pulled in from another `pnr.yaml`, which write and lock that suite's `artefacts/.runs/<tag>/`.
- `--resume latest` and `--dispatch slurm` work inside the tag's tree; checkpoints, stage jobs and orphan checks are per tag.

### What a tagged call reads

A tagged call reads nothing in place from a tree it does not own. It copies each block abstract, and the upstream synthesis netlist and filelist, into its own tree while holding the source tree's lock, and reads only the copies.

- Each read checks the copy against the content it was taken from, and refuses a changed copy, naming the directory to remove so it is copied again.
- Synthesis and P&R in one call read the same copies. A tagged run whose synthesis has no netlist to copy fails at setup.
- A block copy's manifest paths are checked against the block's own project.
- Copies live in `artefacts/.runs/<tag>/.block-abstracts/` and `artefacts/.runs/<tag>/.synth/`, one directory per source and content digest, never overwritten. They use disk until you remove the tag's tree (`rm -r artefacts/.runs/<tag>`), which also removes the results that read them.

A run's `blocks:` are copied from the abstract their block run published under the same tag.

- Only when `rb pnr` under the tag never started that block run is the untagged abstract copied, with `pnr.block_untagged_abstract`. This is checked under the tag tree's lock, so a starting block run is waited for.
- A block run the tag ran that failed, was cleared, or was skipped by `reglvl` in a whole-suite call fails its consumers.
- Result `blocks` entries, and a hardened block's `abstract.manifest.json` `blocks`, record the consumed abstract's `run_tag` (null for untagged) and `manifest_sha256`. A tagged run also writes them to `blocks.consumed.json`, with paths relative to the project root.
- Fix-it commands in block errors carry `--run-tag <tag>` when the abstract is tagged.

### Lock order

1. A tagged call locks its own tree first, so a duplicate call is refused before waiting for anything.
2. It then waits for the tagged tree of each other suite whose block it reads, copies the blocks and releases those trees.
3. It takes trees in one fixed order, releasing its own while waiting for one ordered before it. A consumer may wait for a same-tag call that holds a block's tree while producing it, but same-tag calls never wait for each other in a cycle.
4. Afterwards it holds at most one other tree's lock at a time. A tagged `--synth` copies its synthesis's block abstracts before locking the synthesis tree.

### Synthesis under a tag

Synthesis is not namespaced: it writes the untagged `artefacts/<synth>/`. Untagged, `--synth` locks the synthesis tree for the whole call; tagged, while each synthesis runs and its netlist is copied. When `synth.yaml` shares the directory of `pnr.yaml`, that tree is the untagged P&R tree, so a tagged `--synth` waits for any untagged call there to end.

### Export and read tagged results

`rb pnr-export --run-tag <tag>` exports a tagged result with the block copies recorded in its `blocks.consumed.json`, and fails when `blocks:` no longer lists one.

- It needs only those copies, so the block's `pnr.yaml` may have changed or gone, and the project may have moved or been copied.
- The copies may be in the tagged tree of the suite whose call pulled the run in, which the record names, even in another project.
- A record naming a copy outside the project and that tree, as an absolute path does after a move, is refused; re-run under the tag.
- Each checkpoint run directory keeps the record of the call that wrote it, which `--checkpoint` reads.

`rb power --run-tag <tag>` reads the run from the same tag; see [Power Analysis: Read a tagged P&R run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#read-a-tagged-pr-run). Other readers read the untagged `artefacts/<run>/` only; see [`--run-tag` readers](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#--run-tag-covers-the-test-and-pr-flows-not-every-reader).
