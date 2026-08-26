## randtest

```text
Usage: rtl-buddy randtest [OPTIONS] TEST_NAME [RND_CNT]

 repeat a test with multiple random seeds

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT       name of test [default: (run all tests)] [required]    │
│      rnd_cnt        [RND_CNT]  number of random iterations to test [default: 2]      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config  -c      TEXT     test_config.yaml to use [default: tests.yaml]        │
│ --rnd-rpt      -r      INTEGER  repeat iteration number from previous run            │
│ --dispatch             TEXT     execution backend for the seed fan-out (local,       │
│                                 local-parallel, slurm)                               │
│                                 [default: (cfg-dispatch backend, else local)]        │
│ --jobs         -j      INTEGER  concurrent jobs for --dispatch local-parallel        │
│                                 [default: (cfg-dispatch jobs, else min(4, cpu        │
│                                 count))]                                             │
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
