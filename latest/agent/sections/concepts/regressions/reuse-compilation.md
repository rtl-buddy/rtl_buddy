## Reuse compilation

When tests share compile inputs, reuse a compiled build:

```bash
rb regression --share-build
```

Verilator, VCS, and Icarus support cross-test sharing. See [Sharing compiled builds](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#sharing-compiled-builds-across-tests) for invalidation and backend limitations.
