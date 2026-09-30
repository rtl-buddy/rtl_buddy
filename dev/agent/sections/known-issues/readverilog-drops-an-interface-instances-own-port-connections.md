## read_verilog drops an interface instance's own port connections

Yosys's `read_verilog` cannot bind an interface instance to a child module's interface port. It warns ``Could not find interface instance for `<inst>' in `<module>'`` and exits 0, and the instance's own port connections are lost: `bus_if b (.clk(clk));` leaves `\b.clk` undriven, so every flop clocked from it loses its clock, with plausible area and timing numbers.

- `unresolved-interfaces` gates the warning. `warn` (the default) logs `synth.unresolved_interface` per instance, `error` fails the run, and `allow` skips the scan.
- `frontend: slang` binds the instance properly, but an interface port on the synthesis top itself needs a flat-port wrapper.

See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#gate-unbound-interface-instances).
