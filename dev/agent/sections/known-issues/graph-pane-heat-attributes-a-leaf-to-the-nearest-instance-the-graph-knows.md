## Graph-pane heat attributes a leaf to the nearest instance the graph knows

The `/gph` heat overlay rolls per-instance power up to the enclosing RTL module using the graph's design tier, so it is only as fine as that tier is complete.

- If the run's `top:` is a wrapper the graph was not built for, or the graph was narrowed with `rb graph build --model`, no leaf power can be attributed. The pane paints cells and area only.
- A row whose path runs through a level the graph lacks goes to the deepest level it has, which over-attributes that module.
- A module's power is summed over every instantiation, while its cells and area are counted once.

See [Design Knowledge Graph](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/#physical-heat-on-the-graph).
