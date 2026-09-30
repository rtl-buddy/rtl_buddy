## Reuse compilation

```bash
rb regression --share-build
```

When tests share compile inputs this reuses one build. Reuse is reported once per build directory on the console, and each test's `compile.log` and the log file record their own. `--rebuild` compiles even when the stamp says the build is current. Verilator, VCS and Icarus support sharing; see [Sharing compiled builds](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#sharing-compiled-builds-across-tests) for invalidation and limits.
