## Gate static-lifetime subroutines

A `function` or `task` declared at module, interface, package, program or compilation-unit scope without `automatic` has static lifetime: each formal argument is one shared storage location. Simulation is unaffected, but yosys-slang lowers it literally, so two calls in one combinational process alias their arguments and the netlist is wrong with no error.

Before Yosys starts, rtl_buddy scans the filelist's sources and their `` `include `` headers and reports each declaration as `file:line: function <name>`.

- `error` fails the run before Yosys starts. It is the default with `frontend: slang`.
- `warn` logs one warning per finding and records `static_function_findings` in `--machine` output. It is the default with `frontend: verilog`, which inlines per call site, so the result is correct but not portable.
- `allow` skips the scan.

The scan reports declarations, not actual aliases, so a subroutine with a single call site also fails. Fix by adding the keyword; set `static-functions: warn` while migrating:

```systemverilog
function automatic ptr_t inc(input ptr_t p);
  return p + 1;
endfunction
```

Class methods, `extern` and `pure virtual` prototypes, DPI imports and exports, and anything declared `automatic` are exempt. For testbench and non-synthesisable sources, Verible's `explicit-function-lifetime` rule runs through `rb lint` and `cfg-verible`.

The scan is a tokenizer, not an elaborator. It follows `` `include `` and evaluates `` `ifdef `` on definedness only. It misses declarations produced by macros, under `-y` library directories, or in unresolvable headers. Scope nesting is tracked by keyword pairing, so unusual but legal code can change which declarations count as exempt.
