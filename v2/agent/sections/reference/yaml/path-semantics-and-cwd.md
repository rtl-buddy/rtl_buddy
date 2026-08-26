## Path semantics and cwd

- `rtl_buddy.log`, `logs/`, and the convenience symlinks (`test.log`, `test.err`, `test.randseed`) are written to the current working directory.
- `test` and `randtest` do **not** automatically change into the suite directory. Run from the suite directory, or use `--test-config` with a full path.
- `regression` does `chdir` into each suite directory before executing.
- For portable configs in multi-suite repos, make paths in `tests.yaml` explicit and verify they resolve correctly from the intended invocation directory.
