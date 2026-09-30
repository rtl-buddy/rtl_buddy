## Override a plusarg for one run

`--plusarg KEY=VALUE` adds or replaces a runtime plusarg for one invocation:

```bash
rb test e2e --plusarg mutate=1 --dispatch slurm
```

- `--plusarg KEY` alone is the valueless `+KEY`. Repeat the flag for several; the last value wins over the YAML and earlier flags.
- The override applies to every selected test, reaches the `preproc` hook (`test_cfg.get_plusarg("mutate")`) and the simulator, and is forwarded to `--dispatch` jobs. It never invalidates a shared build.
- A `sweep` hook does not see the override, and a `preproc` hook that sets the same key wins.
- It cannot be combined with `--list`. Overriding the plusarg named by `sim-rand-seed-plusarg` exits 2; use `--master-seed` or `sim-rand-seed`.

Overrides are recorded as `plusarg_overrides` in `result.json`, the summary footer and `--machine` results.
