## Relationship to `rb fpv`

`rb fpv` proves assertions exhaustively up to a bound; `rb test` with `assertions: true` exercises them on the dynamic stimulus your testbench drives. The two are complementary:

- Use simulation to find **the obvious bugs cheaply** — every existing test now polices SVA properties as a side effect of running.
- Use [`rb fpv`](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/fpv/) to **prove invariants** over all reachable behaviors up to the bound.

A property that proves bounded under `rb fpv` and never fires in simulation is the strongest signal you'll get without a commercial completeness tool.
