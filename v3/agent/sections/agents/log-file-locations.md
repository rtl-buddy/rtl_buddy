## Log file locations

| File | Description |
|------|-------------|
| `rtl_buddy.log` | Orchestration log; JSONL in machine mode, human-readable otherwise |
| `artefacts/{test_name}/test.log` | Simulation stdout for each test |
| `artefacts/{test_name}/test.err` | Simulation stderr for each test |
| `artefacts/{test_name}/test.randseed` | Seed used for this test run |
| `artefacts/{test_name}/coverage.dat` | Coverage database (if coverage is enabled) |
| `artefacts/{test_name}/compile.log` | Compile transcript |
| `artefacts/{test_name}/run-NNNN/test.log` | Per-iteration output for `randtest` |
| `test.log` | Symlink to the most recent test's log |
| `test.err` | Symlink to the most recent test's stderr |
| `test.randseed` | Symlink to the most recent test's seed |

All files are written relative to the suite directory where `rtl_buddy` is invoked.
