## How the SDC is read

`rb` reads SDC and XDC text with one of two readers. `rb tool-check` names the active one under `In-process readers`.

- **`tcl`**: a safe Tcl interpreter, so line continuations, braces, nested collections, `$variables` and `[expr ...]` read as they do in Vivado and OpenSTA. It has no `exec`, `open`, `file`, `socket`, `cd`, `glob` or `source`; `source` includes are not followed, so name the included file directly.
- **`tokenizer`**: the fallback when no Tcl interpreter can start, typically a Python without `_tkinter`. It splits words but evaluates nothing, so `-period $p` or `-period [expr ...]` is reported as unevaluated. Install tkinter to restore the `tcl` reader: `uv python install --managed-python`, `brew install python-tk@<X.Y>`, or `dnf install python3-tkinter`.

Either reader returns syntax only. `get_ports`, `get_cells` and `-filter` collections stay opaque names, because OpenROAD or Vivado resolves them against a netlist.

Set `RTL_BUDDY_CONSTRAINT_READER=tokenizer` to force the fallback, or `=tcl` to require the interpreter and fail if none can start.
