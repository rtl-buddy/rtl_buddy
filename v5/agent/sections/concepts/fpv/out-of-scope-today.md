## Out of scope (today)

- **SymbiYosys-only.** Commercial backends (JasperGold, VC Formal, OneSpin) are not yet wired up — adding them parallels the pattern documented for [SpyGlass in `rb cdc`](https://github.com/rtl-buddy/rtl_buddy/issues/85).
- **Per-property granularity.** The summary table reports the overall sby verdict, not per-assertion pass/fail. Sby's own `status.json` per task is preserved under `sby_workdir/` for users who need that detail.
- **Wide SVA coverage.** Yosys's native frontend supports a limited subset of SystemVerilog Assertions. Broader SVA coverage will land alongside the [slang frontend](https://github.com/rtl-buddy/rtl_buddy/issues/88).
