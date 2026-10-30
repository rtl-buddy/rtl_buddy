## Select activity

| Mode and fields | Applied activity |
| --- | --- |
| `mode: static` | No activity command |
| `mode: dynamic` with `activity.saif` | Per-signal SAIF activity |
| `mode: dynamic` with `activity.vcd` | Per-signal VCD activity |
| `mode: dynamic` with no trace | Synthetic toggle rate and static probability, set globally and on the primary inputs |

`activity.saif` and `activity.vcd` are mutually exclusive. Set `activity.scope` only with a trace, to the hierarchy containing the design, such as `tb_top/u_dut`. Synthetic defaults are a 0.1 toggle rate and 0.5 static probability; override them with `activity.default-toggle-rate` and `activity.default-static-prob`. The inputs take the same values because OpenSTA reports `inf` or `NaN` power downstream of an input with no activity that it does not time, such as a false-pathed input.

Before applying any activity, the script runs `sta::update_generated_clks` after `read_sdc`. OpenSTA leaves a generated clock at period 0 until its waveform is derived, and `report_power` does not derive it, so an activity relative to that clock, such as input activity on a port whose `set_input_delay` names a forwarded clock, would otherwise be infinite.
