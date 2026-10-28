## Check and repair slew at output ports

A port carries no max-slew or max-capacitance limit unless the SDC sets one, so the electrical counts and the `electrical` gate pass a long net that ends at an output pin, however slow its pin. `repair_design` does not repair a port's slew even when the SDC sets a limit on it; it checks the net at its driver only. To make outputs part of the electrical check and have P&R fix them, do both:

- In the SDC, set `set_max_transition` on the outputs, for example `set_max_transition 320 [all_outputs]` at the library's limit, and `set_load` for what the pins drive. The violator counts and `electrical.rpt` then include the output pins.
- Buffer the outputs and hold the buffers at their pins:

```yaml
    buffer-ports:
      inputs: false                # default true
      skip-clock-outputs: true     # default false
      hold-outputs-at-pins: true   # default false
```

Each output net then ends at a buffer input, which has a Liberty limit, so placement's `repair_design` resizes the driver or adds repeaters to meet it. Without `hold-outputs-at-pins`, global placement puts each output buffer anywhere between its driver and its pin, and the net from the buffer to the pin is again a net that ends at a port. With it, each buffer goes on the row site nearest its pin that no other held buffer uses, after pin placement, and stays there, fixed (`FIRM`), through global placement. It is freed right after, before `repair_tie_fanout` and the `after-global-placement` hooks, so `repair_design` repairs the driver net and legalization places the buffer. That repair works on placement parasitics, so a long driver net can still route over its limit at the buffer's input; `electrical.rpt` lists it as a max-slew violator on the buffer's input pin.

`buffer_ports` also buffers an output that carries a clock: a forwarded clock on the clock network, or an output a generated clock is defined on. `skip-clock-outputs: true` leaves every such output unbuffered and names each in `pnr.log`.

`buffer-ports: true`, and the default of a `harden: true` run, buffer every input and output as OpenROAD and ORFS do, without the two options. `inputs` and `outputs` map to `buffer_ports -inputs` and `-outputs`. See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/v7/reference/yaml/#pnryaml).
