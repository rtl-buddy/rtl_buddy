## Paint the graph with physical heat

Open `/gph` and tick `heat` to fill the design graph's module nodes with this model's numbers. Cells and area come from the synthesis half by module name. Power is the sum of the leaf rows inside every instantiation of the module, joined by instance path. The pane shows how many instantiations and rows went into each figure. See [Physical Heat on the Graph](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/#physical-heat-on-the-graph).

The graph's run dropdown selects the same runs as this pane, and a `phys-focus` turns the overlay on.
