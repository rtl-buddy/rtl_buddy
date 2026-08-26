## Path and working directory

`test` and `randtest` do **not** automatically change directory to the suite directory. Run them from the directory containing `tests.yaml`, or pass an explicit `--test-config` path.

Paths in `tests.yaml` (such as `model_path`) are resolved relative to the suite file's directory, not the invocation directory.
