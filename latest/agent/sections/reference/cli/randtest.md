## randtest

```text
Usage: rtl-buddy randtest [OPTIONS] TEST_NAME [RND_CNT]

 repeat a test with multiple random seeds

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT       name of test [default: (run all tests)] [required]    │
│      rnd_cnt        [RND_CNT]  number of random iterations to test [default: 2]      │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config                -c      TEXT                test_config.yaml to use     │
│                                                          [default: tests.yaml]       │
│ --rnd-rpt                    -r      INTEGER             repeat iteration number     │
│                                                          from previous run           │
│ --rebuild                                                recompile even when a valid │
│                                                          build already exists        │
│                                                          (implies nothing about      │
│                                                          --share-build)              │
│ --shared-build-root                  TEXT                persistent directory the    │
│                                                          shared builds are cached    │
│                                                          under, so the cache         │
│                                                          survives a workspace wipe   │
│                                                          [default: (cfg-rtl-reg      │
│                                                          shared-build-root, else     │
│                                                          in-tree)]                   │
│ --dispatch                           TEXT                execution backend for the   │
│                                                          seed fan-out (local,        │
│                                                          local-parallel, slurm)      │
│                                                          [default: (cfg-dispatch     │
│                                                          backend, else local)]       │
│ --jobs                       -j      INTEGER             concurrent jobs for         │
│                                                          --dispatch local-parallel   │
│                                                          [default: (cfg-dispatch     │
│                                                          jobs, else min(4, cpu       │
│                                                          count))]                    │
│ --orphans                            TEXT                what to do about an         │
│                                                          interrupted run's jobs that │
│                                                          are still queued or running │
│                                                          (warn, cancel, adopt)       │
│                                                          [default: (cfg-dispatch     │
│                                                          orphans, else warn)]        │
│ --run-tag                            TEXT                namespace this run's        │
│                                                          artefact tree under         │
│                                                          artefacts/.runs/<tag>/ with │
│                                                          its own tree lock and log,  │
│                                                          so concurrent runs of a     │
│                                                          suite do not collide;       │
│                                                          shared builds stay shared   │
│ --coverage-merge                                         merge coverage across the   │
│                                                          seeds; uses raw merge for   │
│                                                          summary/html and            │
│                                                          info-process for Coverview  │
│ --coverage-merge-raw                                     use raw Verilator merge for │
│                                                          merged                      │
│                                                          summary/html/Coverview      │
│ --coverage-merge-info-proc…                              use info-process merge for  │
│                                                          merged summary/Coverview;   │
│                                                          HTML merge is not supported │
│ --coverage-html                                          generate merged LCOV HTML   │
│                                                          output in                   │
│                                                          coverage_merge.html         │
│ --coverage-coverview                                     generate Coverview zip      │
│                                                          output from coverage info   │
│ --coverage-dir-summary               TEXT                append coverage summary     │
│                                                          lines for repo-relative     │
│                                                          directory prefixes; may be  │
│                                                          repeated                    │
│ --coverage-dir-summary-file          TEXT                file containing             │
│                                                          repo-relative directory     │
│                                                          prefixes, one per line      │
│ --coverage-source-summary                                append run coverage scored  │
│                                                          per source point (covered   │
│                                                          when any elaboration hit    │
│                                                          it), beside the             │
│                                                          per-elaboration figure      │
│ --coverage-model                     [full|totals|none]  coverage-model.json to      │
│                                                          write: full (per-seed       │
│                                                          attribution per point),     │
│                                                          totals (points without      │
│                                                          attribution), or none       │
│                                                          (manifest and totals only)  │
│                                                          [default: full]             │
│ --help                                                   Show this message and exit. │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
