## Log file locations

| File | Description |
|------|-------------|
| `rtl_buddy.log` | Orchestration log; JSONL in machine mode, human-readable otherwise |
| `logs/{test_name}.log` | Simulation stdout for each test |
| `logs/{test_name}.err` | Simulation stderr for each test |
| `logs/{test_name}.randseed` | Seed used for this test run |
| `test.log` | Symlink to the most recent test's log |
| `test.err` | Symlink to the most recent test's stderr |
| `test.randseed` | Symlink to the most recent test's seed |

All files are written relative to the directory where `rtl_buddy` is invoked.
