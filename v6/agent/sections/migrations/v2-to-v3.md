## v2 to v3

Per-test outputs moved from `logs/` to `artefacts/` inside the suite directory.

| v2 | v3 |
|----|-----|
| `logs/{test}.log` | `artefacts/{test}/test.log` |
| `logs/{test}.err` | `artefacts/{test}/test.err` |
| `logs/{test}.randseed` | `artefacts/{test}/test.randseed` |
| `logs/{test}.coverage.dat` | `artefacts/{test}/coverage.dat` |
| `logs/{test}.compile.log` | `artefacts/{test}/compile.log` |

`randtest` iterations write to numbered subdirectories (`artefacts/{test}/run-0001/test.log`, …), while shared compile outputs (`compile.log`, `run.f`) stay at `artefacts/{test}/`. The suite-root `test.log` / `test.err` / `test.randseed` symlinks still point at the latest run.

Update `.gitignore`, CI, and coverage scripts from `logs/` to `artefacts/`. Use `artefacts/{test}/coverage.dat` for one run and `artefacts/{test}/run-*/coverage.dat` for randomized runs. Build hook paths from `suite_dir`; v5 changed the hook working directory again.
