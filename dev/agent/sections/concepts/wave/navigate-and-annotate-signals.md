## Navigate and annotate signals

In Surfer, select a signal to establish the active instance scope, then choose **Go to declaration**. RTL Buddy opens the declaration and annotates signals in that scope with their values at the current waveform cursor. Moving the cursor refreshes the annotations.

To annotate only the selected signal:

```bash
rb wave basic --focused-signal
```

With `ctrl-sock` configured, place the nvim cursor on a signal and press `<leader>wa` to add it to Surfer. Select a Surfer signal first so the active scope is unambiguous.

If an nvim socket is stale, the next navigation request starts a new editor instance. If the plugin is missing, `rb wave` warns and continues without annotations.
