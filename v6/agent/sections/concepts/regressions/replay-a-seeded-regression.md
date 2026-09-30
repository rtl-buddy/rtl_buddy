## Replay a seeded regression

One master seed reproduces every selected test's runtime seed:

```bash
rb regression --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

The master seed appears once in the run summary and machine payload. Each test's seed is independent of suite order and dispatch timing, and a dispatched plan carries the seeds to its workers. The same command on the same project layout reproduces the seeds without old artefacts. See [Run with randomized seeds](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#run-with-randomized-seeds).
