## Select activity

| Mode and fields | Applied activity |
| --- | --- |
| `mode: static` | No activity command |
| `mode: dynamic` with `activity.saif` | Per-signal SAIF activity |
| `mode: dynamic` with `activity.vcd` | Per-signal VCD activity |
| `mode: dynamic` with no trace | Global synthetic toggle rate and static probability |

`activity.saif` and `activity.vcd` are mutually exclusive. Set `activity.scope` only with a trace; use the hierarchy containing the design, such as `tb_top/u_dut`.

Synthetic defaults are a 0.1 toggle rate and 0.5 static probability. Override them with `activity.default-toggle-rate` and `activity.default-static-prob`.
