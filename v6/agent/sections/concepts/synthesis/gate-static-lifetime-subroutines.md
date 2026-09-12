## Gate static-lifetime subroutines

A `function` or `task` declared at module, interface, package, program, or
compilation-unit scope without an explicit `automatic` lifetime has *static*
lifetime: every formal argument is one shared storage location. Simulation is
unaffected, because a call completes atomically inside a process, but
yosys-slang lowers the declaration literally and gives every call site the same
net per formal. Two calls in one combinational process then alias their
arguments and the netlist is wrong with no error and no warning.

RTL Buddy scans the sources named by the synthesis filelist, and the headers
they `` `include ``, before Yosys starts, and reports each declaration as
`file:line: function <name>`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      static-functions: error      # error | warn | allow
      conflicting-drivers: error   # error | allow
```

`static-functions` defaults to `error` with `frontend: slang`, which
miscompiles the design, and to `warn` with the legacy `verilog` frontend, which
inlines per call site — correct there, but not portable. `error` fails the run
before Yosys; `warn` logs one warning per finding and records
`static_function_findings` in the result envelope; `allow` skips the scan.

Fix a finding by adding the keyword:

```systemverilog
function automatic ptr_t inc(input ptr_t p);
  return p + 1;
endfunction
```

### What the scan sees

`` `include `` directives are followed, resolved against the including file's
directory and then the filelist's `+incdir+` entries — the same directories
`rb synth` hands Yosys as `read_verilog -I` / `read_slang -I`, each resolved
against the filelist that declared it. Each inclusion is scanned
in its own context — a header included from a class is exempt there and still
reported when the same header is included from an ordinary module — and one
declaration is reported once however many places include it.

`` `ifdef `` / `` `ifndef `` / `` `elsif `` / `` `else `` / `` `endif `` are
evaluated on definedness and updated by `` `define ``, `` `undef `` and
`` `undefineall `` in the sources. The macro table is seeded to match the Yosys
invocation exactly:

- the filelist's `+define+` entries and then the run's `defines:`, which is
  what `rb synth` passes to `read_verilog -D` / `read_slang -D`;
- the macros the selected frontend defines for itself, which differ:
  `read_verilog` predefines `SYNTHESIS` and `YOSYS`, while `read_slang`
  predefines `SYNTHESIS` and slang's own built-ins (`__slang__`, the
  `SV_COV_*` constants) but **not** `YOSYS`. So a `` `ifndef SYNTHESIS ``
  simulation-only helper is never reported under either, an
  `` `ifdef SYNTHESIS `` region always is, and a `` `ifndef YOSYS `` helper is
  reported only under `frontend: slang`, which is the frontend that compiles
  it.

Filelist `+define+` entries come first so the synth.yaml entry's `defines:`
win on conflict. A bare `+define+X` takes the value the
selected frontend gives a valueless `-D`: tools disagree about what such a
macro expands to — Verilator and Yosys's `read_verilog` give it an empty body,
while Icarus and slang give it `1` — so write `+define+X=1` if a value is meant.
A run whose `defines:` override a filelist entry with a different value (or any
value, for a bare entry) logs one `synth.filelist_defines_overridden` warning
naming both values: simulation then elaborates with the filelist's value and
synthesis with the synth.yaml one. Drop one of the two if the flows are meant
to agree. A filelist `+define+` whose value contains whitespace is fatal: a
Yosys script line is split on whitespace and no quoting survives, so no `-D`
can carry it.

`` `undefineall `` follows the frontend in use, which differ: slang clears the
source's own macros but re-applies the command-line ones, so the seed above
survives; Yosys's `read_verilog` clears its command-line cache as well, so
nothing does. A `` `ifndef `` guarded on a `defines:` macro after an
`` `undefineall `` is therefore compiled under `frontend: verilog` and not
under `frontend: slang`, and the scan reports it accordingly.

An `` `include `` chain deeper than 1024 — slang's own limit — fails the run
with the tail of the chain named, rather than silently skipping the header and
losing whatever it declares.

The macro table follows the compilation-unit boundary the frontend actually
uses. With `single-unit: false` — the default — each source is its own
compilation unit, so a `` `define `` in one file does not reach the next and
the table is re-seeded from the filelist and run defines for each; `single-unit: true`
under `frontend: slang` shares it, matching `read_slang --single-unit`. A
header always shares its includer's table, because `` `include `` is textual.

`(* ... *)` attributes are ignored wholesale, so an identifier inside one
never qualifies the declaration it decorates.

Exempt: class methods — including out-of-body definitions such as
`function int C::f(...)`, though not an escaped `\C::f`, which is one
identifier — `extern` and `pure virtual` prototypes, DPI imports
and exports, and any scope declared `module automatic` (or
`package`/`interface`/`program` `automatic`).

The scan is a tokenizer, not an elaborator, and its limits run in both
directions:

| Limit | Effect |
| --- | --- |
| Macro bodies are skipped at their `` `define `` | A declaration produced by a macro is never reported, in either direction; macros are expanded by the compiler, not by the scan |
| `-y` library directories are not scanned | The filelist never names their contents, so declarations there are missed |
| An unresolvable `` `include `` is logged at DEBUG and skipped | That header's declarations are missed; the run is not failed |
| An unknown `frontend` or a missing slang plugin | Fails the run as a configuration error (exit 2) before the gates, not as a synthesis `FAIL` |
| `` `if `` expression evaluation is not implemented | Only definedness is evaluated. This is not SystemVerilog anyway, so it costs nothing in practice |
| Scope nesting is tracked by keyword pairing | Pathological but legal code can change which declarations are exempt, in either direction |

Use Verible's `explicit-function-lifetime` rule through `rb lint` and
`cfg-verible` as the style-lint complement covering testbench and
non-synthesisable sources.
