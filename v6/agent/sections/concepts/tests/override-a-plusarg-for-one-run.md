## Override a plusarg for one run

Add or replace a runtime plusarg for a single invocation instead of editing `tests.yaml`:

```bash
rb test e2e --plusarg mutate=1 --dispatch slurm
```

`--plusarg KEY=VALUE` merges over each selected test's `plusargs:`, and a bare `--plusarg KEY` is the valueless `+KEY`. Repeat the flag for several plusargs; the last value given wins over both the YAML and an earlier `--plusarg`. The override applies to every test the invocation selects, reaches the `preproc` hook (`test_cfg.get_plusarg("mutate")`) as well as the simulator command line, and travels through `--dispatch` to every job. It cannot be combined with `--list`, which runs nothing.

Use it for a throwaway variation: a deliberate-fault ("negative control") pass proving each bench can fail, a bumped `+timeout_us` while debugging, or a `+prog_dir` pointing at a hand-built binary. Plusargs are runtime-only, so an override never invalidates a shared build. There is no compile-time counterpart, because overriding a plusdefine would change the compile key and force a rebuild.

Each run records its own overrides as `plusarg_overrides` in `result.json` and in the summary footer and `--machine` results, so a one-off run stays distinguishable from the configured entry. A `sweep` hook does not see the override — it is merged after expansion — and a `preproc` hook that sets the same key still wins. Overriding the plusarg a test's `sim-rand-seed-plusarg` names exits 2: rtl_buddy rewrites that plusarg from the resolved seed, so choose the seed with `--master-seed` or `sim-rand-seed` instead.
