## Reuse compilation

When tests share compile inputs, reuse a compiled build:

```bash
rb regression --share-build
```

Verilator, VCS, and Icarus support cross-test sharing. Reuse is reported once per build directory per process on the console, and every test's `compile.log` (and the log file) records its own reuse; add `--rebuild` to compile even when the stamp says the build is current. See [Sharing compiled builds](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#sharing-compiled-builds-across-tests) for invalidation and backend limitations.
