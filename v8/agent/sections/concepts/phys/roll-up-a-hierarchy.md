## Roll up a hierarchy

The model stores leaf values only. `rb phys instance <path>` sums the leaves under the path at query time.

- `match` is `exact` for a path that names a row and `prefix` for a subtree.
- A path that is both a row and a prefix of other rows rolls up to the row alone. The rows below it are listed and counted in `child_count` for navigation, and the console says they are not in the total.
- The rollup adds the four power columns only. The model has no per-cell area, so area comes from `rb phys module`.
