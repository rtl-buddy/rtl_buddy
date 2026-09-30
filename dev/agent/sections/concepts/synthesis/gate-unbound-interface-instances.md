## Gate unbound interface instances

`read_verilog` cannot bind an interface instance to a child module's interface port. It warns ``Could not find interface instance for `<instance>' in `<module>'`` and exits 0. The instance's own port connections are dropped, so an interface that carries a clock or reset leaves it undriven:

```systemverilog
bus_if b (.clk(clk));          // .clk is dropped
producer u_p (.b(b), ...);     // flops in both children
consumer u_c (.b(b), ...);     // lose their clock
```

`frontend: slang` binds the instance correctly. Otherwise `unresolved-interfaces` decides:

- `warn` (default) logs one `synth.unresolved_interface` per instance, records `unresolved_interfaces` in `--machine` output, and passes. The result is correct for interfaces with no ports of their own.
- `error` fails the run and removes the netlist, so `rb pnr` and `rb power` cannot consume it. Use it in projects whose interfaces have ports.
- `allow` silences the warning.
