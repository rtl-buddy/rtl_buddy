## Path and working directory

`test` and `randtest` anchor outputs on the directory containing `tests.yaml`. You can run them from anywhere — invoke `rb test -c path/to/tests.yaml` and the artifact tree, `rtl_buddy.log`, and builder scratch all land under `dirname(tests.yaml)`, not your shell's cwd. See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/) for the full picture and the worked example for invoking from a sibling directory.

Paths in `tests.yaml` (such as `model_path`) are resolved relative to the suite file's directory, not the invocation directory.

Plusargs are passed to the simulator verbatim. If a plusarg should reference a suite-local file, resolve it explicitly in preproc using `suite_dir`. Bare output filenames can remain artifact-relative so they land under `artefacts/{test_name}/`.
