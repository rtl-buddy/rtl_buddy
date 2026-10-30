## Run several P&R calls at once

`rb pnr` locks the suite's whole `artefacts/` tree, so a second untagged `rb pnr` on the same `pnr.yaml` is refused, even for a different run. Give each concurrent call its own `--run-tag`, for example to sweep one block's settings:

```bash
rb pnr alu_pnr -c pnr/alu/pnr.yaml --run-tag util55 &
rb pnr alu_pnr -c pnr/alu/pnr.yaml --run-tag util60 &
wait
rb pnr-export alu_pnr -c pnr/alu/pnr.yaml --run-tag util60 --png
```

- A tagged call writes each run under `artefacts/.runs/<tag>/<run>/`, with its own tree lock and `rtl_buddy.log` in `artefacts/.runs/<tag>/`. Tag rules are in [Namespace concurrent runs](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/execution-context/#namespace-concurrent-runs).
- Two calls under the same tag, or two untagged, contend for one lock: the second is refused. Calls under different tags run side by side, and so do a tagged call and an untagged one, except while the tagged call copies an input from the untagged tree (below). It then waits for that tree's lock, logging `artifact_lock.wait` every five minutes.
- A tagged call reads nothing in place from a tree it does not own. It copies each block abstract and the upstream synthesis netlist and filelist into its own tree while holding the lock of the tree they are in, checks a block copy's views against the abstract's manifest whenever it reads the copy, and reads only the copies. Synthesis and P&R in one call read the same copies. Besides its own tree, it holds one other tree's lock at a time, so a tagged `--synth` copies its synthesis's block abstracts before it locks the synthesis tree. A tagged run whose synthesis has no netlist to copy fails at setup. A block copy's manifest paths are checked against the project the block came from.
- A run's `blocks:` are copied from the abstract their block run published under the same tag. Only when `rb pnr` under the tag has never started that block run (checked under the lock of the tag's tree, so a block run that is starting is waited for) is the untagged abstract copied instead, with the warning `pnr.block_untagged_abstract`. A block run that the tag ran but that failed, was cleared, or was skipped by `reglvl` in a whole-suite call fails its consumers.
- Copies live in `artefacts/.runs/<tag>/.block-abstracts/` and `artefacts/.runs/<tag>/.synth/`, one directory per source and content digest, and are never overwritten. They take disk space until you remove the tag's tree (`rm -r artefacts/.runs/<tag>`), which also removes the results that read them.
- Each result's `blocks` entries, and a hardened block's `abstract.manifest.json` `blocks`, record the `run_tag` the consumed abstract was published under (null for untagged) and its `manifest_sha256`. A tagged run also writes them to `blocks.consumed.json` in its directory. Fix-it commands in block errors carry `--run-tag <tag>` when the abstract they name is a tagged one.
- With no run name, the tag also applies to runs pulled in from another `pnr.yaml`: they write and lock that suite's `artefacts/.runs/<tag>/`.
- Synthesis is not namespaced: it writes the untagged `artefacts/<synth>/`. Untagged, `--synth` locks the synthesis suite's tree for the whole call. Tagged, it locks it while each synthesis runs and its netlist is copied. When `synth.yaml` shares the directory of `pnr.yaml`, that tree is the untagged P&R tree, so a tagged `--synth` waits for any untagged call there to end before synthesizing.
- `--resume latest` and `--dispatch slurm` work inside the tag's tree: checkpoints, stage jobs and orphan checks are per tag.
- `rb pnr-export --run-tag <tag>` exports a tagged result with the block copies that run consumed, recorded in its `blocks.consumed.json`, and fails when `blocks:` no longer lists one of them. It needs only those copies, so the block's `pnr.yaml` may have changed or gone since. Each checkpoint run directory keeps the record of the call that wrote it, which `--checkpoint` reads. `rb power` and other readers read the untagged `artefacts/<run>/` only; see [`--run-tag` readers](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#--run-tag-covers-the-test-and-pr-flows-not-every-reader).
