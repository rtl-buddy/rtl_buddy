## Read a model with only one half

A synthesis fills the model's `modules` half and a power run fills its `instances` half. The halves meet in one artefact directory, for the same top and netlist, in either order. To pair them, set `phys-run: <synth run>` in `power.yaml` so the power run publishes beside that synthesis. See [Pair the model with a synthesis run](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#pair-the-model-with-a-synthesis-run), [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#inspect-artefacts) and [Power Analysis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#inspect-artefacts).

With one half absent, every verb still answers from the half present and names the command that produces the other.

- The merge requires both halves to record the same netlist sha256. When it refuses, the power run warns and publishes its half alone; rerun the synthesis in that directory to pair them.
- A power half from a routed database (`netlist-source: pnr`) has no netlist to hash and cannot be paired. Synthesise, then rerun `rb power` on the netlist the synthesis wrote.
- `rb phys instance` needs the power half. Without it the verb exits 2 and points at `rb power`.
