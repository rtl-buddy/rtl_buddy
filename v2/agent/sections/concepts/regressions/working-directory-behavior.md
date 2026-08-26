## Working directory behavior

Unlike `test`, the `regression` subcommand **changes directory** into each suite directory before running its tests. This means relative paths in `tests.yaml` (such as `model_path`) are resolved correctly without any extra setup.

Run `regression` from the repo root so that the paths in `regressions.yaml` resolve correctly.
