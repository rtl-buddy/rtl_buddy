## Check in-process readers

Not every behavior that can change under the same `rb` install comes from an external binary. `rb tool-check` therefore ends with an `In-process readers` section:

```text
In-process readers
----------------------------------------------------------------------
  constraint reader: tcl (Tcl 9.0.3)
```

`constraint reader` is the backend SDC/XDC files are read with: `tcl` (a safe Tcl interpreter, run in a short-lived worker process so `rb` itself never loads `tkinter` — see [How the SDC is read](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#how-the-sdc-is-read) for why — reported with the Tcl version that worker found) or `tokenizer` (the stdlib-only word splitter used when no worker can start an interpreter, typically a Python without `_tkinter`). The difference is visible in results — the tokenizer does not evaluate `$variables` or `[expr ...]` — so a run that reads constraints reports which one answered. `RTL_BUDDY_CONSTRAINT_READER=tokenizer|tcl` pins it. See [How the SDC is read](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#how-the-sdc-is-read).

The JSON payload carries the same value as `readers.constraints`, without the Tcl version.
