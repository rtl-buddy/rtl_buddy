## Path and working directory

`test` and `randtest` do **not** automatically change directory to the suite directory. Run them from the directory containing `tests.yaml`, or pass an explicit `--test-config` path.

Paths in `tests.yaml` (such as `model_path`) are resolved relative to the suite file's directory, not the invocation directory.

Plusargs are passed to the simulator verbatim. If a plusarg should reference a suite-local file, resolve it explicitly in preproc using `suite_dir`. Bare output filenames can remain artifact-relative so they land under `artefacts/{test_name}/`.
