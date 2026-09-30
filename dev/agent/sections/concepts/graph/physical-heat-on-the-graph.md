## Physical Heat on the Graph

The `/gph` pane can fill module nodes with the physical model that `rb synth` and `rb power` write, the same data as the [`/phy` pane](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#browse-the-model-in-the-hub).

- Tick `heat` in the header. The model loads on the first tick.
- `metric` offers `cells`, `area`, `leakage`, `dynamic` and `total`. `run` selects the artefact directory; `/gph?dir=<phys dir>` opens the pane on one run. A run the server refuses (no manifest, or outside the project) leaves the current model and shows the refusal in the status line.
- Each node shows its value in a badge, every metric in its tooltip and the full row in the inspector. Coverage and heat share the node fill, so enabling one turns off the other.

Cells and area are counted once per module definition; power is summed over every instantiation. Do not add cells or area to power, and do not divide power by area. Power is attributed by instance path, so an incomplete design tier degrades it; see [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#graph-pane-heat-attributes-a-leaf-to-the-nearest-instance-the-graph-knows).

A `phys-focus` from a script turns heat on, loads the model if needed, and highlights the node:

```bash
rb hub send phys-focus module:dma_engine --metric area
```
