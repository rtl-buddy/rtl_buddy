## Check and repair slew at output ports

By default slow output pins pass every check. A port has no max-slew or max-capacitance limit unless the SDC sets one, so the electrical counts and gate pass a long net to an output pin however slow it is. `repair_design` does not repair a port's slew even with an SDC limit, since it checks the net at its driver only.

To check and fix outputs, do both:

1. In the SDC, set `set_max_transition` on the outputs at the library's limit (for example `set_max_transition 320 [all_outputs]`) and `set_load` for what they drive. The violator counts and `electrical.rpt` then include output pins.
2. Buffer the outputs and hold the buffers at their pins:

```yaml
    buffer-ports:
      inputs: false                # default true
      skip-clock-outputs: true     # default false
      hold-outputs-at-pins: true   # default false
```

Each output net then ends at a buffer input with a Liberty limit, which placement's `repair_design` meets by resizing the driver or adding repeaters.

- Without `hold-outputs-at-pins`, global placement may put the buffer anywhere between driver and pin, and the buffer-to-pin net again ends at a port.
- With it, after pin placement each buffer goes on the row site nearest its pin that no other held buffer uses, fixed (`FIRM`) through global placement.
- It is freed right after, before `repair_tie_fanout` and the `after-global-placement` hooks, so `repair_design` repairs the driver net and legalization places the buffer.
- That repair uses placement parasitics, so a long driver net can still route over its limit. `electrical.rpt` lists it as a max-slew violator on the buffer's input.

`buffer_ports` also buffers outputs that carry a clock: a forwarded clock on the clock network, or an output with a generated clock. `skip-clock-outputs: true` leaves them unbuffered and names each in `pnr.log`.

`buffer-ports: true`, the default of a `harden: true` run, buffers every input and output as OpenROAD and ORFS do, without the two options. `inputs` and `outputs` map to `buffer_ports -inputs` and `-outputs` ([YAML Formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#pnryaml)).
