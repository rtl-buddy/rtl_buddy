## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed; `--rnd-last` reuses it. `randtest` runs repeated seeded iterations. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/#randtest) for replay and selection options.

Use one explicit master seed when a test or regression must replay without
depending on old artefacts:

```bash
rb test smoke --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

RTL Buddy derives each runtime seed from the master seed, the
project-root-relative `tests.yaml` path, the sweep-expanded test name, and the
run ID when present. Test selection, ordering, checkout location, and dispatch
timing do not change it. Repeating the command with the same master seed
replays the same seeds. Master seeds are nonnegative integers and may exceed
the simulator's seed range; derived simulator seeds are from 1 through
2147483647.

Configure `sim-rand-seed-plusarg` when a preprocessor generates randomized
stimulus:

```yaml
tests:
  - name: smoke
    # ...
    sim-rand-seed-plusarg: stimulus_seed
```

The resolved value is available through
`test_cfg.get_resolved_seed()` and
`test_cfg.get_plusarg("stimulus_seed")` before `preproc` runs. RTL Buddy then
passes that value to the simulator, restores the configured plusarg if the
hook changed it, writes it to `test.randseed` and `result.json`, and includes
it in structured logs and machine results. Runtime seed values and plusargs do
not change the compile key.

Without a master or fixed test seed, the plusarg receives the builder's default
integer unchanged, including `0`; the positive 31-bit limit applies only to
fixed test seeds and master-derived seeds. `--rnd-new` and `--rnd-last` are
rejected for a test that configures this plusarg because those modes select their value too late for preprocessing.
`randtest` therefore requires a fixed `sim-rand-seed` for such a test; its one
shared preprocessor run and every iteration receive that fixed value.

Set `sim-rand-seed` on a test whose timing or command-cycle stimulus must stay
fixed:

```yaml
tests:
  - name: command_timing
    # ...
    sim-rand-seed: 41
    sim-rand-seed-plusarg: stimulus_seed
```

The fixed value overrides the invocation's master, new, or replay seed policy.
Mutation simulation oracles also resolve fixed seeds and exposed builder defaults
before preprocessing, for both the baseline and every mutant.
See [YAML Formats: tests.yaml](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#testsyaml) for the field
contract.
