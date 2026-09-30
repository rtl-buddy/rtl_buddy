## In-process readers

The report ends with an `In-process readers` section:

```text
In-process readers
----------------------------------------------------------------------
  constraint reader: tcl (Tcl 9.0.3)
```

`constraint reader` is the backend that reads SDC/XDC files:

- `tcl`: a safe Tcl interpreter in a short-lived worker process.
- `tokenizer`: a word splitter used when no interpreter can start, typically a Python without `_tkinter`. It does not evaluate `$variables` or `[expr ...]`.

`RTL_BUDDY_CONSTRAINT_READER=tokenizer|tcl` pins the choice. See [How the SDC is read](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#how-the-sdc-is-read).
