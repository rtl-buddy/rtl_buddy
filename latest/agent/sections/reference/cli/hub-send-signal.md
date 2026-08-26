## hub send signal

```text
Usage: rtl-buddy hub send signal [OPTIONS] SIGNAL

 Broadcast signal_selected{signal, wave_scope}.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    signal      TEXT  signal name, e.g. wr_ptr_q [required]                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --wave-scope        TEXT  surfer/VCD scope owning the signal, e.g. tb.dut.u_fifo  │
│                              [required]                                              │
│    --help                    Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
