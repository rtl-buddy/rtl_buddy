## Path semantics and cwd

- `rtl_buddy.log` and the convenience symlinks (`test.log`, `test.err`, `test.randseed`) are written to the suite root (the current working directory).
- Per-test artifacts are written to `artefacts/{test_name}/` under the suite root. Single runs write `test.log`, `test.err`, `test.randseed`, `compile.log`, `run.f`, and (if enabled) `coverage.dat` there directly. Repeated runs (`randtest`) write sim outputs into numbered subdirectories: `artefacts/{test_name}/run-0001/`, etc.
- `test` and `randtest` do **not** automatically change into the suite directory. Run from the suite directory, or use `--test-config` with a full path.
- `regression` does `chdir` into each suite directory before executing.
- Preproc plusargs are passed to the simulator verbatim. Resolve suite-local input paths explicitly against `suite_dir`; keep output filenames artifact-relative when they should land under `artefacts/{test_name}/`.
- For portable configs in multi-suite repos, make paths in `tests.yaml` explicit and verify they resolve correctly from the intended invocation directory.

---
