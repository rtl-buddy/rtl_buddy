## How the SDC is read

`rb` reads SDC and XDC text with one of two readers. `rb tool-check` names the active one under `In-process readers`.

- **`tcl`**: a safe Tcl interpreter, so line continuations, braces, nested collections, `$variables` and `[expr ...]` read as they do in Vivado and OpenSTA. It has no `exec`, `open`, `file`, `socket`, `cd`, `glob` or `source`; `source` includes are not followed, so name the included file directly. The synthesis clock-period read is the exception below.
- **`tokenizer`**: the fallback when no Tcl interpreter can start, typically a Python without `_tkinter`. It splits words but evaluates nothing, so `-period $p` or `-period [expr ...]` is reported as unevaluated. Install tkinter to restore the `tcl` reader: `uv python install --managed-python`, `brew install python-tk@<X.Y>`, or `dnf install python3-tkinter`.

Either reader returns syntax only. `get_ports`, `get_cells` and `-filter` collections stay opaque names, because OpenROAD or Vivado resolves them against a netlist.

When `rb synth` reads the SDC for ABC's delay target, it also:

- follows `source`, relative to the sourcing file's directory and then the working directory. `[info script]` names the file being read, and `file dirname`, `join`, `normalize`, `tail` and the other path subcommands answer as Tcl's do, so `source [file join [file dirname [file normalize [info script]]] clocks.sdc]` works. The `tokenizer` follows only a literal path. An include it cannot read warns `constraints.source_unreadable`.
- re-reads a file the interpreter refused with `get_ports`, `get_pins`, `get_cells`, `get_nets`, `get_clocks` and `all_*` returning their name arguments as a list, so a `proc` or `foreach` over `[get_ports {a b}]` runs.

Set `RTL_BUDDY_CONSTRAINT_READER=tokenizer` to force the fallback, or `=tcl` to require the interpreter and fail if none can start.
