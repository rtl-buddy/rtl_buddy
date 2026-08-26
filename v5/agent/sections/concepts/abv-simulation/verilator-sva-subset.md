## Verilator SVA subset

Verilator implements a **subset** of IEEE 1800-2017 §16. Today's expectations:

- ✅ Immediate assertions: `assert (cond);`
- ✅ Concurrent assertions on synchronous properties: `always @(posedge clk) assert property (a |-> b);`
- ✅ Cover properties: `cover property (...)` — hits flow into the existing `--coverage-user` pipeline and are merged through `--coverage-merge` like any other user coverage point.
- ⚠️ `disable iff` clauses — not supported.
- ⚠️ Local variables inside properties — not supported.
- ⚠️ Full sequence operators — partial. `##N`, `[*N]`, `|->`, `|=>` work; advanced operators like `intersect`, `throughout`, `within` are not supported.

For a property set that needs the full SVA language, point those properties at `rb fpv` (which can use the slang frontend) or fall back to a commercial simulator. See the [Verilator language support notes](https://verilator.org/guide/latest/languages.html) for the authoritative list.
