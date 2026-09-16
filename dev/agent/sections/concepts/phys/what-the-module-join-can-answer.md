## What the module join can answer

The two halves spell `module` in two namespaces. In the synthesis half it is an RTL module name, as Yosys' `stat` saw it. In the power half it is the Liberty cell each leaf instance is an instance of — `DFF_X1`, `NAND2_X1` — because a mapped netlist's leaves are cells, not RTL modules.

So `rb phys module` answers Liberty-cell questions: `rb phys module DFF_X1` reports every flop instance and the power they burn together.

It does not attribute power to an RTL module, and flattening the design does not change that. The join matches the power half's `module` field as it stands, and that field holds the cell a leaf is an instance of — so no leaf row carries `u_cpu`'s name, or the top's, and the join finds nothing. When a name resolves out of the synthesis half alone and the power half is populated, the payload's `instance_join` says so in words and the console prints it, so an empty instance list is never mistaken for "this block burns nothing". Ask what a block burns by its instance path instead: `rb phys instance u_cpu` sums the leaf rows under it — see [Roll up a hierarchy](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#roll-up-a-hierarchy).

Nothing stops one name from being in both namespaces — a Liberty cell named after a block, or an RTL module called `DFF_X1`. Then the two halves are measuring two different things under one word, and `rb phys module` says so: `namespaces` lists both and `instance_join` carries a collision note the console prints. The row and the instances are still reported, and still not added together; the note is what keeps a module's cells and area beside a cell type's power from reading as one block's totals.

See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#rb-phys-module-reports-no-power-for-an-rtl-module).
