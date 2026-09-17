## randtest

```text
Usage: rtl-buddy randtest [OPTIONS] TEST_NAME [RND_CNT]

 repeat a test with multiple random seeds

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT       name of test [default: (run all tests)] [required]    │
│      rnd_cnt        [RND_CNT]  number of random iterations to test [default: 2]      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config        -c      TEXT     test_config.yaml to use [default: tests.yaml]  │
│ --rnd-rpt            -r      INTEGER  repeat iteration number from previous run      │
│ --rebuild                             recompile even when a valid build already      │
│                                       exists (implies nothing about --share-build)   │
│ --shared-build-root          TEXT     persistent directory the shared builds are     │
│                                       cached under, so the cache survives a          │
│                                       workspace wipe                                 │
│                                       [default: (cfg-rtl-reg shared-build-root, else │
│                                       in-tree)]                                      │
│ --dispatch                   TEXT     execution backend for the seed fan-out (local, │
│                                       local-parallel, slurm)                         │
│                                       [default: (cfg-dispatch backend, else local)]  │
│ --jobs               -j      INTEGER  concurrent jobs for --dispatch local-parallel  │
│                                       [default: (cfg-dispatch jobs, else min(4, cpu  │
│                                       count))]                                       │
│ --orphans                    TEXT     what to do about an interrupted run's jobs     │
│                                       that are still queued or running (warn,        │
│                                       cancel, adopt)                                 │
│                                       [default: (cfg-dispatch orphans, else warn)]   │
│ --help                                Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
