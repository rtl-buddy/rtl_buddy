## Navigate and annotate signals

In Surfer, select a signal to set the active instance scope, then choose **Go to declaration**. The editor opens the declaration and annotates the signals in that scope with their values at the waveform cursor. Moving the cursor refreshes the annotations.

To annotate only the selected signal:

```bash
rb wave basic --focused-signal
```

With `ctrl-sock` set, put the nvim cursor on a signal and press `<leader>wa` to add it to Surfer. Select a Surfer signal first so the scope is unambiguous.

If the nvim socket is stale, the next navigation request starts a new editor. If the plugin is missing, `rb wave` warns and continues without annotations.
