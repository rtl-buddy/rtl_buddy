## What the module join can answer

The two halves use `module` in different namespaces. In the synthesis half it is an RTL module name. In the power half it is the Liberty cell each leaf instance is an instance of, such as `DFF_X1` or `NAND2_X1`.

`rb phys module` therefore answers Liberty-cell questions: `rb phys module DFF_X1` reports every flop instance and their combined power.

It cannot attribute power to an RTL module such as `u_cpu`, because no leaf row carries that name. When a name resolves in the synthesis half only, the console says so. An empty instance list does not mean the block burns nothing; use `rb phys instance u_cpu`, which sums the leaf rows under that path. See [Roll up a hierarchy](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/#roll-up-a-hierarchy) and [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/#rb-phys-module-reports-no-power-for-an-rtl-module).

A name can exist in both namespaces, such as an RTL module called `DFF_X1`. `rb phys module` then lists both in `namespaces`, prints a collision note, and reports the module row and the cell instances without adding them together.
