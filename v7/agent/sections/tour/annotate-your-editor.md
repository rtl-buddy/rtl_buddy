## Annotate your editor

The Neovim plugin is the editor side of the hub. `rb nvim-install` clones [rtl-buddy-nvim](https://github.com/rtl-buddy/rtl-buddy-nvim) and wires it in with one command; the plugin composes with `verible-verilog-ls` rather than replacing it.

```bash
uv run rb nvim-install
```

Two annotations appear without any hub running. `rb wave` launches Neovim with the selected signal's value as virtual text at its declaration, and `<leader>wa` adds the signal under the cursor to the Surfer waveform:

![Neovim showing the tb_top testbench with the selected signal's value 8'h46 rendered inline after the declaration of y](../static/img/tour/nvim-wave-annotation.png)

Every `module` declaration carries its synthesized cell count and area, read from the same physical model the `/phy` pane shows, through one `rb --machine phys summary` call per project:

![Neovim showing demo_tiny_alu_subsys_compute with 84 cells and 468.4 µm² rendered inline after the module declaration](../static/img/tour/nvim-phys-annotation.png)

With the hub connected, a click on a schematic instance opens its file at the right line, `:RtlBuddyShow` pans the schematic and Surfer to the cursor, and `:RtlBuddyToWave` adds the symbol under the cursor to the waveform. Both captures above are the real Neovim screen grid, rendered through the UI protocol. See [Install the nvim integration](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/wave/#install-the-nvim-integration).
