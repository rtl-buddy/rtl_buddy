## Roll up a hierarchy

The model records leaf values only, because a subtree sum depends on the hierarchy the consumer projects onto. `rb phys instance <path>` is that consumer: it sums the leaves under the path at query time and leaves the document unchanged.

A path that names a row exactly is answered by that row. `match` reports which question was answered — `exact` or `prefix` — and the rollup answers that question and no other: the named row for an exact match, every leaf under the path for a prefix one. A path that is both a row and a prefix of other rows therefore rolls up to the row alone; the rows below it are still listed and still counted by `child_count`, and the console says they are there for navigation rather than in the total.

The rollup adds the four power columns directly, and reports nothing else. It carries no area: the model has no per-cell area to sum, and the only substitute available — joining each leaf's module to the synthesis half — would add the whole module's area once per leaf, on a name that may belong to the other namespace entirely. Area is reported per RTL module, by `rb phys module`, against the name Yosys counted it for.
