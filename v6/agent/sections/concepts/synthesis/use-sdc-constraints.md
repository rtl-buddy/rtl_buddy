## Use SDC constraints

A Yosys run extracts `create_clock` periods from the SDC and supplies the shortest period to ABC. It warns when multiple clocks require this approximation.

An OpenROAD run loads the complete SDC and reports actual worst and total negative slack. Use it for multi-clock timing decisions.

### How the SDC is read

`rb` reads SDC and XDC text with one of two backends, and `rb tool-check` names the active one under `In-process readers`:

- `tcl` — a real Tcl interpreter (a `-safe` child interpreter), so `\` continuations, braces, nested `[get_pins [get_cells u]/C]` collections, `;` separators, `$variables` and `[expr ...]` all read the way Vivado and OpenSTA read them. The file is *evaluated*, but a safe interpreter has no `exec`, `open`, `file`, `socket`, `cd`, `glob` or `source`, so a constraint file cannot spawn a process or touch the filesystem, and a resource limit stops one that tries to loop forever. `source` includes are not followed: read the included file directly.

  The interpreter runs in a **short-lived worker process** (one per constraint file, 50-90 ms), not inside `rb`. Python's only embedded Tcl is `tkinter`, and loading it starts a Tcl notifier thread that never exits; on macOS a later `fork` + `exec` from such a process can wedge the forked child in `close()` indefinitely, which would show up as `rb synth` or `rb pnr` hanging while launching a tool after reading an SDC. Keeping the interpreter out of process removes that hazard entirely.
- `tokenizer` — the fallback used when no worker can start a Tcl interpreter: the running Python has no `_tkinter` (a Homebrew Python without `python-tk`, or a distro Python without `python3-tkinter`), or its Tcl library is unusable. It splits words correctly but evaluates nothing, so a `-period $p` or `-period [expr ...]` is reported as unevaluated rather than read as a number. Installing tkinter (`uv python install --managed-python`, `brew install python-tk@<X.Y>`, `dnf install python3-tkinter`) restores the interpreter backend.

Everything the reader returns is *syntax*: `get_ports` / `get_cells` / `-filter` collections stay opaque names either way, because resolving them needs a linked netlist that OpenROAD and Vivado own.

Set `RTL_BUDDY_CONSTRAINT_READER=tokenizer` to force the fallback, or `=tcl` to require the interpreter (which fails loudly where no worker can start one, instead of quietly downgrading).
