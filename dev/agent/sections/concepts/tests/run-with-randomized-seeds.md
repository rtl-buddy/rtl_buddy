## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed and `--rnd-last` reuses it. `randtest` runs repeated seeded iterations; see the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/#randtest) for replay and selection options.

To replay a test or regression without old artefacts, pass one master seed:

```bash
rb test smoke --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

- Each runtime seed is derived from the master seed, the `tests.yaml` path relative to the project root, the sweep-expanded test name and the run ID. Selection, ordering, checkout location and dispatch timing do not change it.
- A master seed is a nonnegative integer. Derived seeds, and a fixed `sim-rand-seed`, are 1 through 2147483647.

### Seed a preprocessor

For a `preproc` hook that generates random stimulus, name the plusarg that carries the seed:

```yaml
tests:
  - name: smoke
    sim-rand-seed-plusarg: stimulus_seed
```

- Before `preproc` runs, `test_cfg.get_resolved_seed()` and `test_cfg.get_plusarg("stimulus_seed")` return the resolved seed. The simulator gets that value even if the hook changed the plusarg.
- The seed is written to `test.randseed`, `result.json` and machine results.
- With no master or fixed seed, the plusarg gets the builder's default integer, including `0`.
- `--rnd-new` and `--rnd-last` are rejected for such a test. `randtest` needs a fixed `sim-rand-seed`.

Set `sim-rand-seed` to keep stimulus fixed:

```yaml
tests:
  - name: command_timing
    sim-rand-seed: 41
    sim-rand-seed-plusarg: stimulus_seed
```

A fixed seed overrides the invocation's master, new or replay policy, including for mutation baselines and mutants.
