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
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
