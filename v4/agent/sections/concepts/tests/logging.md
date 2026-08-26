## Logging

`rtl_buddy` writes orchestration output to `rtl_buddy.log` in the directory where it is invoked.

Per-test simulation output goes to `artefacts/{test_name}/`:

- `test.log` — full simulation output
- `test.err` — stderr
- `test.randseed` — the seed used
- `coverage.dat` — coverage database (if coverage is enabled)
- `compile.log` — compile transcript
- `run.f` — generated filelist

For repeated runs (`randtest`), each iteration writes into a numbered subdirectory — `artefacts/{test_name}/run-0001/`, `run-0002/`, etc. — while compile outputs remain at the top of `artefacts/{test_name}/`.

The symlinks `test.log`, `test.err`, and `test.randseed` at the suite root always point to the most recent run.

For machine-readable logs (JSON Lines), use `--machine`. See [For Agents](https://rtl-buddy.github.io/rtl_buddy/v4/agents/).
