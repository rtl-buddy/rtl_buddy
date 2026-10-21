## Execution Contexts

Use explicit contexts, never ambient `os.getcwd()`:

- `invocation_cwd`: the directory where the user ran `rb`. Use it to resolve relative CLI arguments before they become absolute.
- `command_root`: the directory containing the command's primary config file.
- `suite_dir`: the command root for per-suite flows such as `tests.yaml`, `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, and `fpga.yaml`.
- `artifact_dir`: the generated workspace for one command item, normally `suite_dir/artefacts/<name>`.

Config-driven commands use their primary config's directory as `command_root`. Managed outputs go below it, external tools run from their artifact directory, and explicit CLI paths resolve from `invocation_cwd`.

`--run-tag <name>` namespaces one invocation's artefact root to `<command_root>/artefacts/.runs/<name>/`. `test`, `randtest`, `regression`, the dispatch job commands, and `graph results` accept it.

- Every per-run path in the table below, and the command's `rtl_buddy.log`, moves below the tagged root.
- Shared builds (`artefacts/.shared-builds/`, keyed on the compile fingerprint) and `graph.json` are shared and never namespaced.
- Classify every new path builder under `artefacts/` as per-run or shared. Per-run paths go through `run_artifact_root()` or `test_artifact_dir(..., run_tag=...)`; shared paths do not.
- Without `--run-tag`, the flat layout is unchanged.
