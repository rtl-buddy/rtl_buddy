## Replay a seeded regression

Pass one master seed to reproduce every selected test's runtime seed:

```bash
rb regression --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

The master seed appears once in the run summary and machine payload. Each
test's resolved seed is independent of suite order and dispatch timing, and a
dispatched plan carries both values to its worker. Repeating the command with
the same project layout and master seed reproduces the seeds without reading
old artefacts. See [Run with randomized seeds](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#run-with-randomized-seeds)
for derivation, preprocessor access, fixed-test overrides, and result records.
