"""Tests for the pre-synthesis static-lifetime scan."""

from textwrap import dedent

import pytest

from rtl_buddy.tools.sv_lifetime_scan import (
    LifetimeFinding,
    describe_findings,
    scan_file,
    scan_files,
    scan_text,
)


def _names(findings):
    return [(f.line, f.kind, f.name) for f in findings]


# Repro

# `inc` (line 9) and `same` (line 10) lack `automatic`, so yosys-slang shares one net per formal between the call sites.
_BAD_SV = dedent("""\
    module bad (
      input  logic clk, rst, psh, pop,
      output logic emp, ale,
      output logic [3:0] wa, ra
    );
      typedef logic [4:0] ptr_t;
      ptr_t wptr, rptr, wptr_i, rptr_i;

      function ptr_t inc(input ptr_t p);       return p + ptr_t'('d1); endfunction
      function bit   same(input ptr_t a, b);   return (a == b);        endfunction

      always_comb begin
        wptr_i = inc(wptr);
        rptr_i = inc(rptr);
        emp    = same(.a(rptr),   .b(wptr));
        ale    = same(.a(rptr_i), .b(wptr));
      end

      always_ff @(posedge clk) begin
        if (rst) begin wptr <= '0; rptr <= '0; end
        else begin
          if (psh)         wptr <= wptr + 5'd1;
          if (pop && !emp) rptr <= rptr + 5'd1;
        end
      end
      assign wa = wptr_i[3:0];
      assign ra = rptr_i[3:0];
    endmodule
""")

_GOOD_SV = _BAD_SV.replace("function ptr_t", "function automatic ptr_t").replace(
    "function bit   same", "function automatic bit same"
)


def test_issue_repro_bad_reports_both_functions_with_line_numbers():
    findings = scan_text(_BAD_SV, "bad.sv")
    assert _names(findings) == [(9, "function", "inc"), (10, "function", "same")]
    assert findings[0].path == "bad.sv"


def test_issue_repro_good_reports_nothing():
    assert scan_text(_GOOD_SV, "good.sv") == []


def test_issue_repro_describe_names_file_line_and_function():
    findings = scan_text(_BAD_SV, "bad.sv")
    assert findings[0].describe() == "bad.sv:9: function inc"


# Lifetime resolution


def test_explicit_static_function_is_a_finding():
    src = dedent("""\
        module m;
          function static int f(input int a);
            return a;
          endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "f")]


def test_task_without_automatic_is_a_finding():
    src = dedent("""\
        module m;
          task run(input int a);
          endtask
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "task", "run")]


def test_automatic_task_is_exempt():
    src = dedent("""\
        module m;
          task automatic run(input int a);
          endtask
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_module_automatic_scope_exempts_unqualified_functions():
    src = dedent("""\
        module automatic m;
          function int f(input int a);
            return a;
          endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_package_automatic_scope_exempts_unqualified_functions():
    src = dedent("""\
        package automatic p;
          function int f(input int a);
            return a;
          endfunction
        endpackage
    """)
    assert scan_text(src, "p.sv") == []


def test_package_without_lifetime_still_reports():
    src = dedent("""\
        package p;
          function int f(input int a);
            return a;
          endfunction
        endpackage
    """)
    assert _names(scan_text(src, "p.sv")) == [(2, "function", "f")]


def test_interface_and_program_automatic_scopes_are_exempt():
    src = dedent("""\
        interface automatic i;
          function int f; return 1; endfunction
        endinterface
        program automatic pr;
          function int g; return 1; endfunction
        endprogram
    """)
    assert scan_text(src, "s.sv") == []


def test_module_scope_closes_so_the_next_module_is_independent():
    src = dedent("""\
        module automatic a;
          function int f; return 1; endfunction
        endmodule
        module b;
          function int g; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "s.sv")) == [(5, "function", "g")]


def test_compilation_unit_scope_function_is_a_finding():
    src = "function int f(input int a); return a; endfunction\n"
    assert _names(scan_text(src, "u.sv")) == [(1, "function", "f")]


# Exemptions


def test_class_methods_are_exempt():
    src = dedent("""\
        class C;
          function new(); endfunction
          function int f(input int a); return a; endfunction
          task run(); endtask
          static function int g(); return 1; endfunction
          virtual function int h(); return 1; endfunction
        endclass
    """)
    assert scan_text(src, "c.sv") == []


def test_class_inside_a_package_stays_exempt_and_the_package_still_reports():
    src = dedent("""\
        package p;
          class C;
            function int f; return 1; endfunction
          endclass
          function int g; return 1; endfunction
        endpackage
    """)
    assert _names(scan_text(src, "p.sv")) == [(5, "function", "g")]


def test_typedef_class_forward_declaration_does_not_open_a_scope():
    src = dedent("""\
        package p;
          typedef class C;
          function int g; return 1; endfunction
        endpackage
    """)
    assert _names(scan_text(src, "p.sv")) == [(3, "function", "g")]


def test_extern_and_pure_virtual_prototypes_are_exempt():
    src = dedent("""\
        virtual class C;
          extern function int f(input int a);
          pure virtual function int g(input int a);
        endclass
        module m;
          function int h; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "c.sv")) == [(6, "function", "h")]


def test_dpi_import_and_export_are_exempt():
    src = dedent("""\
        module m;
          import "DPI-C" function int c_add(input int a, input int b);
          import "DPI-C" context function void c_ctx();
          import "DPI-C" pure function int c_pure(input int a);
          export "DPI-C" function sv_cb;
          function int sv_cb; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(6, "function", "sv_cb")]


def test_virtual_interface_variable_does_not_open_an_interface_scope():
    src = dedent("""\
        module m;
          virtual interface bus_if h;
          function int f; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


def test_interface_port_in_a_module_header_does_not_open_a_scope():
    src = dedent("""\
        module m (interface bus, input logic clk);
          function int f; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "f")]


def test_interface_class_is_treated_as_a_class():
    src = dedent("""\
        interface class IC;
          pure virtual function int f();
        endclass
        module m;
          function int g; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "s.sv")) == [(5, "function", "g")]


# Tokenizer robustness


def test_keyword_inside_comments_is_not_a_finding():
    src = dedent("""\
        module m;
          // function int commented_out; return 1; endfunction
          /* function int blocked;
             return 1;
             endfunction */
          function int real_one; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(6, "function", "real_one")]


def test_keyword_inside_a_string_literal_is_not_a_finding():
    src = dedent("""\
        module m;
          initial $display("function int in_a_string; endfunction");
          function int real_one; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "real_one")]


def test_line_numbers_survive_a_multi_line_block_comment():
    src = dedent("""\
        module m;
          /*
           * still a comment
           */
          function int f; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(5, "function", "f")]


def test_packed_range_return_type_does_not_confuse_the_name():
    src = dedent("""\
        module m;
          function bit [WIDTH-1:0] widened(input int a);
            return a;
          endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "widened")]


def test_scope_resolved_return_type_does_not_confuse_the_name():
    src = dedent("""\
        module m;
          function pkg::state_e decode(input int a);
            return pkg::IDLE;
          endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "decode")]


def test_void_function_declared_without_a_port_list():
    src = dedent("""\
        module m;
          function void bump;
          endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "bump")]


def test_nested_function_inside_an_automatic_function_is_exempt():
    """Pins the scan's behaviour on a nested function, which SystemVerilog does not allow."""
    src = dedent("""\
        module m;
          function automatic int outer(input int a);
            function int inner(input int b);
              return b;
            endfunction
            return inner(a);
          endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_generate_and_case_blocks_do_not_disturb_scope_tracking():
    src = dedent("""\
        module m;
          generate
            if (1) begin : g
              function int f; return 1; endfunction
            end
          endgenerate
          function int h; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [
        (4, "function", "f"),
        (7, "function", "h"),
    ]


# File-level helpers


def test_scan_file_reads_from_disk(tmp_path):
    src = tmp_path / "bad.sv"
    src.write_text(_BAD_SV)
    findings = scan_file(str(src))
    assert _names(findings) == [(9, "function", "inc"), (10, "function", "same")]
    assert findings[0].path == str(src)


def test_scan_file_missing_path_returns_no_findings(tmp_path):
    assert scan_file(str(tmp_path / "nope.sv")) == []


def test_scan_files_concatenates_in_order(tmp_path):
    a = tmp_path / "a.sv"
    b = tmp_path / "b.sv"
    a.write_text("module a; function int f; return 1; endfunction endmodule\n")
    b.write_text("module b; function int g; return 1; endfunction endmodule\n")
    findings = scan_files([str(a), str(b)])
    assert [f.name for f in findings] == ["f", "g"]


def test_describe_findings_truncates_and_counts_the_remainder():
    findings = [
        LifetimeFinding(path="a.sv", line=i, kind="function", name=f"f{i}")
        for i in range(1, 13)
    ]
    text = describe_findings(findings, limit=3)
    assert text.startswith("a.sv:1: function f1; a.sv:2: function f2; ")
    assert text.endswith("and 9 more")


# `include following


def test_include_relative_to_the_including_file_is_scanned(tmp_path):
    """Declarations in an included header still count."""
    (tmp_path / "fns.svh").write_text(
        "function ptr_t inc(input ptr_t p);     return p + 1; endfunction\n"
        "function bit   same(input ptr_t a, b); return (a == b); endfunction\n"
    )
    top = tmp_path / "bad.sv"
    top.write_text(
        dedent("""\
            module bad;
              typedef logic [4:0] ptr_t;
            `include "fns.svh"
            endmodule
        """)
    )
    findings = scan_files([str(top)])
    assert _names(findings) == [(1, "function", "inc"), (2, "function", "same")]
    assert all(f.path.endswith("fns.svh") for f in findings)


def test_include_resolved_through_an_incdir(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "fns.svh").write_text("function int f; return 1; endfunction\n")
    top = tmp_path / "top.sv"
    top.write_text('module m;\n`include "fns.svh"\nendmodule\n')
    assert scan_files([str(top)]) == []
    findings = scan_files([str(top)], incdirs=[str(inc)])
    assert _names(findings) == [(1, "function", "f")]


def test_including_file_directory_wins_over_an_incdir(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "fns.svh").write_text("function int from_incdir; return 1; endfunction\n")
    (tmp_path / "fns.svh").write_text("function int adjacent; return 1; endfunction\n")
    top = tmp_path / "top.sv"
    top.write_text('module m;\n`include "fns.svh"\nendmodule\n')
    findings = scan_files([str(top)], incdirs=[str(inc)])
    assert [f.name for f in findings] == ["adjacent"]


def test_a_header_included_from_two_sources_is_scanned_once(tmp_path):
    (tmp_path / "fns.svh").write_text("function int f; return 1; endfunction\n")
    for name in ("a.sv", "b.sv"):
        (tmp_path / name).write_text(
            f'module {name[0]};\n`include "fns.svh"\nendmodule\n'
        )
    findings = scan_files([str(tmp_path / "a.sv"), str(tmp_path / "b.sv")])
    assert [f.name for f in findings] == ["f"]


def test_nested_includes_are_followed(tmp_path):
    (tmp_path / "inner.svh").write_text("function int deep; return 1; endfunction\n")
    (tmp_path / "outer.svh").write_text('`include "inner.svh"\n')
    top = tmp_path / "top.sv"
    top.write_text('module m;\n`include "outer.svh"\nendmodule\n')
    assert [f.name for f in scan_files([str(top)])] == ["deep"]


def test_unresolvable_include_is_skipped_and_debug_logged(tmp_path, caplog):
    import logging

    top = tmp_path / "top.sv"
    top.write_text(
        'module m;\n`include "nowhere.svh"\n'
        "  function int f; return 1; endfunction\nendmodule\n"
    )
    with caplog.at_level(logging.DEBUG):
        findings = scan_files([str(top)])
    # The rest of the file is still scanned.
    assert [f.name for f in findings] == ["f"]
    events = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", "") == "synth.lifetime_scan_include_unresolved"
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.DEBUG
    assert events[0].rtl_fields["include"] == "nowhere.svh"


def test_include_inside_an_inactive_ifdef_is_not_followed(tmp_path):
    (tmp_path / "fns.svh").write_text("function int f; return 1; endfunction\n")
    top = tmp_path / "top.sv"
    top.write_text(
        dedent("""\
            module m;
            `ifdef NEVER
            `include "fns.svh"
            `endif
            endmodule
        """)
    )
    assert scan_files([str(top)]) == []


def test_a_self_including_header_terminates(tmp_path):
    top = tmp_path / "loop.sv"
    top.write_text(
        'module m;\n`include "loop.sv"\n'
        "  function int f; return 1; endfunction\nendmodule\n"
    )
    assert [f.name for f in scan_files([str(top)])] == ["f"]


# Conditional compilation


def test_ifndef_region_excluded_by_a_run_define_is_not_reported():
    """A sim-only helper behind `ifndef SYNTHESIS is skipped."""
    src = dedent("""\
        module ifd;
        `ifndef SYNTHESIS
          function bit dbg(input bit x); return x; endfunction
        `endif
          function int real_one; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv", defines={"SYNTHESIS": 1})) == [
        (5, "function", "real_one")
    ]
    assert _names(scan_text(src, "m.sv")) == [
        (3, "function", "dbg"),
        (5, "function", "real_one"),
    ]


def test_ifdef_else_takes_exactly_one_branch():
    src = dedent("""\
        module m;
        `ifdef FAST
          function int fast_path; return 1; endfunction
        `else
          function int slow_path; return 1; endfunction
        `endif
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv", defines={"FAST": 1})] == [
        "fast_path"
    ]
    assert [f.name for f in scan_text(src, "m.sv")] == ["slow_path"]


def test_elsif_chain_takes_the_first_matching_branch():
    src = dedent("""\
        module m;
        `ifdef A
          function int a_fn; return 1; endfunction
        `elsif B
          function int b_fn; return 1; endfunction
        `else
          function int c_fn; return 1; endfunction
        `endif
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv", defines={"B": 1})] == ["b_fn"]
    assert [f.name for f in scan_text(src, "m.sv", defines={"A": 1, "B": 1})] == [
        "a_fn"
    ]
    assert [f.name for f in scan_text(src, "m.sv")] == ["c_fn"]


def test_nested_ifdef_inside_an_inactive_region_stays_inactive():
    src = dedent("""\
        module m;
        `ifdef NEVER
          `ifdef ALWAYS
            function int hidden; return 1; endfunction
          `endif
        `endif
          function int visible; return 1; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv", defines={"ALWAYS": 1})] == [
        "visible"
    ]


def test_define_in_a_source_seeds_a_later_ifdef():
    src = dedent("""\
        `define HAVE_IT 1
        module m;
        `ifdef HAVE_IT
          function int taken; return 1; endfunction
        `endif
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["taken"]


def test_undef_reverses_a_define():
    src = dedent("""\
        `define X 1
        `undef X
        module m;
        `ifdef X
          function int taken; return 1; endfunction
        `endif
          function int always_here; return 1; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["always_here"]


def _two_sources_sharing_a_define(tmp_path):
    (tmp_path / "a.sv").write_text("`define SHARED 1\n")
    (tmp_path / "b.sv").write_text(
        "module b;\n`ifndef SHARED\n"
        "  function int hidden; return 1; endfunction\n`endif\nendmodule\n"
    )
    return [str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]


def test_defines_do_not_carry_across_sources_by_default(tmp_path):
    """Without `--single-unit` each file is its own compilation unit, so a macro defined in a.sv is not defined in b.sv."""
    paths = _two_sources_sharing_a_define(tmp_path)
    assert [f.name for f in scan_files(paths)] == ["hidden"]


def test_defines_carry_across_sources_under_single_unit(tmp_path):
    paths = _two_sources_sharing_a_define(tmp_path)
    assert scan_files(paths, single_unit=True) == []


def test_single_unit_does_not_leak_a_define_backwards(tmp_path):
    """Under `--single-unit`, a `define in the second file does not affect a guard in the first."""
    (tmp_path / "a.sv").write_text(
        "module a;\n`ifndef LATE\n"
        "  function int early; return 1; endfunction\n`endif\nendmodule\n"
    )
    (tmp_path / "b.sv").write_text("`define LATE 1\n")
    paths = [str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]
    assert [f.name for f in scan_files(paths, single_unit=True)] == ["early"]


def test_run_defines_reseed_every_source(tmp_path):
    """The run's `defines:` apply to every file."""
    for name in ("a.sv", "b.sv"):
        (tmp_path / name).write_text(
            f"module {name[0]};\n`ifndef SYNTHESIS\n"
            "  function int hidden; return 1; endfunction\n`endif\nendmodule\n"
        )
    paths = [str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]
    assert scan_files(paths, defines={"SYNTHESIS": 1}) == []
    assert len(scan_files(paths)) == 2


def test_undef_in_one_source_does_not_reach_the_next(tmp_path):
    (tmp_path / "a.sv").write_text("`undef SYNTHESIS\n")
    (tmp_path / "b.sv").write_text(
        "module b;\n`ifndef SYNTHESIS\n"
        "  function int hidden; return 1; endfunction\n`endif\nendmodule\n"
    )
    paths = [str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]
    # Each file is seeded from the run defines, so the `undef in a.sv does not reach b.sv.
    assert scan_files(paths, defines={"SYNTHESIS": 1}) == []
    # Under single-unit it does.
    assert [
        f.name for f in scan_files(paths, defines={"SYNTHESIS": 1}, single_unit=True)
    ] == ["hidden"]


# Macro bodies


def test_declaration_inside_a_define_body_is_not_reported():
    """A declaration inside a macro body is not reported at the `define."""
    src = dedent("""\
        `define MK_FN(n) function int n; return 1; endfunction
        module m;
          function int real_one; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "real_one")]


def test_multi_line_define_body_is_skipped_whole():
    src = dedent("""\
        `define MK_FN(n) \\
          function int n; \\
            return 1; \\
          endfunction
        module m;
          function int real_one; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(6, "function", "real_one")]


def test_an_active_multiline_define_body_hides_its_own_conditional():
    """A `ifdef inside a live macro body does not open a conditional."""
    src = (
        "`define WRAPPED \\\n"
        "  `ifdef ALSO_NEVER \\\n"
        "    function int hidden; return 1; endfunction \\\n"
        "  `endif\n"
        "module m;\n"
        "  function int real_one; return 1; endfunction\n"
        "endmodule\n"
    )
    assert [f.name for f in scan_text(src, "m.sv")] == ["real_one"]


def _inactive_define(body: str) -> str:
    """Source with a `define inside a never-taken branch, whose macro body is `body`."""
    return (
        "`ifdef NEVER\n"
        "`define OPENER \\\n"
        f"{body}\n"
        "`endif\n"
        "module m;\n"
        "  function int later; return 1; endfunction\n"
        "endmodule\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "  `ifdef ALSO_NEVER",
        "  `ifndef ALSO_NEVER",
        "  `ifdef ALSO_NEVER \\\n    junk \\\n  `endif",
        "  `else",
        "  `elsif ALSO_NEVER",
        "  `endif",
    ],
)
def test_directives_in_an_inactive_macro_body_do_not_move_the_conditional(body):
    """A `ifdef in a continued macro body inside a disabled branch does not open a conditional; later declarations are still reported."""
    assert [f.name for f in scan_text(_inactive_define(body), "m.sv")] == ["later"]


def test_the_else_branch_of_an_inactive_define_is_still_compiled():
    """The `else of a disabled branch containing a macro body is scanned as live."""
    src = (
        "`ifdef NEVER\n"
        "`define OPENER \\\n"
        "  `ifdef ALSO_NEVER\n"
        "`else\n"
        "function int taken; return 1; endfunction\n"
        "`endif\n"
        "module m;\n"
        "  function int later; return 1; endfunction\n"
        "endmodule\n"
    )
    assert [f.name for f in scan_text(src, "m.sv")] == ["taken", "later"]


def test_an_inactive_define_does_not_register_its_macro_name():
    """A `define in a skipped branch does not define the macro."""
    src = (
        "`ifdef NEVER\n"
        "`define OPENER \\\n"
        "  `ifdef ALSO_NEVER\n"
        "`endif\n"
        "module m;\n"
        "`ifdef OPENER\n"
        "  function int from_defined; return 1; endfunction\n"
        "`endif\n"
        "`ifndef OPENER\n"
        "  function int from_undefined; return 1; endfunction\n"
        "`endif\n"
        "endmodule\n"
    )
    assert [f.name for f in scan_text(src, "m.sv")] == ["from_undefined"]


def test_escaped_identifier_is_never_read_as_a_keyword():
    src = dedent("""\
        module m;
          import "DPI-C" function void \\begin (input int a);
          function int f; return 1; endfunction
        endmodule
    """)
    # The escaped \\begin must not clear the pending `import "DPI-C"` window.
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


def test_escaped_identifier_named_like_a_scope_keyword_is_inert():
    src = dedent("""\
        module m;
          wire \\endmodule ;
          function int f; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


# Out-of-body class method definitions


@pytest.mark.parametrize(
    "decl",
    [
        "function int C::f(input int a); return a; endfunction",
        "task C::t(input int a); endtask",
        "function void a.bar(input int x); endfunction",
        "task i1.t2; endtask",
        "function automatic int D::g(); return 1; endfunction",
    ],
)
def test_out_of_body_definitions_are_exempt(decl):
    src = (
        f"module m;\n  {decl}\n  function int plain; return 1; endfunction\nendmodule\n"
    )
    assert [f.name for f in scan_text(src, "m.sv")] == ["plain"]


def test_a_scope_resolved_return_type_is_still_reported():
    """`pkg::t_e` is a return type, so the function is a module-scope declaration and is reported."""
    src = dedent("""\
        module m;
          function pkg::state_e decode(input int a); return 0; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "decode")]


# Every inclusion is judged in its own context


def test_a_header_included_in_a_class_then_a_module_is_still_reported(tmp_path):
    """A header included from an exempt (class) context is still scanned when included from a hazardous (module) context."""
    (tmp_path / "fns.svh").write_text("function int helper; return 1; endfunction\n")
    top = tmp_path / "top.sv"
    top.write_text(
        dedent("""\
            class C;
            `include "fns.svh"
            endclass
            module m;
            `include "fns.svh"
            endmodule
        """)
    )
    findings = scan_files([str(top)])
    assert _names(findings) == [(1, "function", "helper")]
    assert findings[0].path.endswith("fns.svh")


def test_a_header_included_in_module_automatic_then_a_plain_module(tmp_path):
    (tmp_path / "fns.svh").write_text("function int helper; return 1; endfunction\n")
    top = tmp_path / "top.sv"
    top.write_text(
        dedent("""\
            module automatic a;
            `include "fns.svh"
            endmodule
            module b;
            `include "fns.svh"
            endmodule
        """)
    )
    assert _names(scan_files([str(top)])) == [(1, "function", "helper")]


def test_a_header_exempt_in_every_context_reports_nothing(tmp_path):
    (tmp_path / "fns.svh").write_text("function int helper; return 1; endfunction\n")
    top = tmp_path / "top.sv"
    top.write_text(
        dedent("""\
            module automatic a;
            `include "fns.svh"
            endmodule
            class C;
            `include "fns.svh"
            endclass
        """)
    )
    assert scan_files([str(top)]) == []


def test_a_header_included_by_many_modules_reports_once(tmp_path):
    """A header scanned per inclusion yields one finding per declaration."""
    (tmp_path / "fns.svh").write_text("function int helper; return 1; endfunction\n")
    for name in ("a.sv", "b.sv", "c.sv"):
        (tmp_path / name).write_text(
            f'module {name[0]};\n`include "fns.svh"\nendmodule\n'
        )
    paths = [str(tmp_path / n) for n in ("a.sv", "b.sv", "c.sv")]
    assert _names(scan_files(paths)) == [(1, "function", "helper")]


def test_distinct_declarations_in_one_header_are_all_kept(tmp_path):
    (tmp_path / "fns.svh").write_text(
        "function int one; return 1; endfunction\n"
        "function int two; return 2; endfunction\n"
    )
    top = tmp_path / "top.sv"
    top.write_text('module m;\n`include "fns.svh"\nendmodule\n')
    assert _names(scan_files([str(top)])) == [
        (1, "function", "one"),
        (2, "function", "two"),
    ]


def test_two_headers_with_the_same_basename_are_both_scanned(tmp_path):
    """Two headers with the same basename are both scanned."""
    for sub, fn in (("x", "from_x"), ("y", "from_y")):
        d = tmp_path / sub
        d.mkdir()
        (d / "fns.svh").write_text(f"function int {fn}; return 1; endfunction\n")
        (d / f"{sub}.sv").write_text(f'module {sub};\n`include "fns.svh"\nendmodule\n')
    paths = [str(tmp_path / "x" / "x.sv"), str(tmp_path / "y" / "y.sv")]
    assert sorted(f.name for f in scan_files(paths)) == ["from_x", "from_y"]


# Parameterised return types


@pytest.mark.parametrize(
    "decl",
    [
        "function R#(int) C::f(); return null; endfunction",
        "task T#(4) D::t(); endtask",
        "function automatic q#(2) E::g(); return 0; endfunction",
        "function pkg::box#(int, 8) F::h(); return null; endfunction",
        "function R #(int) C::spaced(); return null; endfunction",
    ],
)
def test_parameterised_return_type_on_an_out_of_body_definition_is_exempt(decl):
    """A `C::` method with a parameterised return type `R#(int)` is exempt."""
    src = (
        f"module m;\n  {decl}\n  function int plain; return 1; endfunction\nendmodule\n"
    )
    assert [f.name for f in scan_text(src, "m.sv")] == ["plain"]


def test_parameterised_return_type_on_a_free_function_still_reports_its_name():
    """A module-scope function with a parameterised return type is reported under its own name."""
    src = dedent("""\
        module m;
          function R#(int) make_box(input int a); return null; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "make_box")]


def test_nested_parameterisation_is_skipped_whole():
    src = dedent("""\
        module m;
          function box#(pair#(int, bit), 4) build(input int a); return null; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "build")]


def test_an_unclosed_parameterisation_does_not_hang_the_scan():
    """A truncated header terminates."""
    src = "module m;\n  function R#(int make_box(input int a);\n"
    assert isinstance(scan_text(src, "m.sv"), list)


def test_a_hash_that_is_not_a_parameterisation_is_ignored():
    """`#` introduces a delay; only `#(` parameterises."""
    src = dedent("""\
        module m;
          function int delayed(input int a); return a; endfunction
          initial #5 $display("x");
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "delayed")]


# `undefineall` keeps the -D macros in slang and clears them in read_verilog.

_UNDEFINEALL_SRC = dedent("""\
    module m;
    `define GUARD 1
    `undefineall
    `ifndef GUARD
      function bit dbg(input bit x); return x; endfunction
    `endif
    endmodule
""")

_UNDEFINEALL_CMDLINE_SRC = dedent("""\
    module m;
    `undefineall
    `ifndef CMDLINE
      function bit dbg(input bit x); return x; endfunction
    `endif
    endmodule
""")


@pytest.mark.parametrize("keeps_predefines", [True, False])
def test_undefineall_clears_a_source_defined_macro(keeps_predefines):
    """`undefineall drops a `define from the source, so the guarded function is compiled and reported."""
    findings = scan_text(
        _UNDEFINEALL_SRC,
        "m.sv",
        undefineall_keeps_predefines=keeps_predefines,
    )
    assert _names(findings) == [(5, "function", "dbg")]


def test_undefineall_spares_a_command_line_define_under_slang():
    assert (
        scan_text(
            _UNDEFINEALL_CMDLINE_SRC,
            "m.sv",
            defines={"CMDLINE": 1},
            undefineall_keeps_predefines=True,
        )
        == []
    )


def test_undefineall_drops_a_command_line_define_under_read_verilog():
    findings = scan_text(
        _UNDEFINEALL_CMDLINE_SRC,
        "m.sv",
        defines={"CMDLINE": 1},
        undefineall_keeps_predefines=False,
    )
    assert _names(findings) == [(4, "function", "dbg")]


def test_undefineall_spares_the_implicit_synthesis_macro_under_slang():
    src = _UNDEFINEALL_CMDLINE_SRC.replace("CMDLINE", "SYNTHESIS")
    assert scan_text(src, "m.sv", defines={"SYNTHESIS": "1"}) == []


def test_undefineall_drops_the_implicit_synthesis_macro_under_read_verilog():
    src = _UNDEFINEALL_CMDLINE_SRC.replace("CMDLINE", "SYNTHESIS")
    findings = scan_text(
        src,
        "m.sv",
        defines={"SYNTHESIS": "1"},
        undefineall_keeps_predefines=False,
    )
    assert [f.name for f in findings] == ["dbg"]


def test_a_define_after_undefineall_takes_effect_again():
    src = dedent("""\
        module m;
        `undefineall
        `define GUARD 1
        `ifndef GUARD
          function bit dbg(input bit x); return x; endfunction
        `endif
          function int always_here; return 1; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["always_here"]


def test_undefineall_inside_an_inactive_region_is_not_applied():
    src = dedent("""\
        module m;
        `define GUARD 1
        `ifdef NEVER
        `undefineall
        `endif
        `ifndef GUARD
          function bit dbg(input bit x); return x; endfunction
        `endif
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_undefineall_in_a_header_reaches_the_includer(tmp_path):
    """A header's `undefineall clears the includer's macros."""
    (tmp_path / "reset.svh").write_text("`undefineall\n")
    top = tmp_path / "top.sv"
    top.write_text(
        dedent("""\
            module m;
            `define GUARD 1
            `include "reset.svh"
            `ifndef GUARD
              function bit dbg(input bit x); return x; endfunction
            `endif
            endmodule
        """)
    )
    assert [f.name for f in scan_files([str(top)])] == ["dbg"]


def test_undefineall_does_not_leak_between_sources_without_single_unit(tmp_path):
    """A trailing `undefineall in one file does not affect the next."""
    (tmp_path / "a.sv").write_text("`undefineall\nmodule a; endmodule\n")
    (tmp_path / "b.sv").write_text(
        "module b;\n`ifndef SYNTHESIS\n"
        "  function int hidden; return 1; endfunction\n`endif\nendmodule\n"
    )
    paths = [str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]
    assert scan_files(paths, defines={"SYNTHESIS": "1"}) == []


def test_undefineall_reaches_the_next_source_under_single_unit(tmp_path):
    """Under `--single-unit` the -D macros survive `undefineall."""
    (tmp_path / "a.sv").write_text("`define LOCAL 1\n`undefineall\n")
    (tmp_path / "b.sv").write_text(
        "module b;\n`ifndef LOCAL\n"
        "  function int hidden; return 1; endfunction\n`endif\n"
        "`ifndef SYNTHESIS\n"
        "  function int also_hidden; return 1; endfunction\n`endif\nendmodule\n"
    )
    paths = [str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]
    findings = scan_files(paths, defines={"SYNTHESIS": "1"}, single_unit=True)
    assert [f.name for f in findings] == ["hidden"]


# Include depth is bounded loudly, never silently


def _include_chain(tmp_path, length, *, leaf_body):
    """Build a chain in which `f0.sv` includes `f1.svh`, and so on down to the leaf."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    for i in range(length):
        nxt = f"f{i + 1}.svh"
        (tmp_path / (f"f{i}.sv" if i == 0 else f"f{i}.svh")).write_text(
            f'`include "{nxt}"\n'
        )
    (tmp_path / f"f{length}.svh").write_text(leaf_body)
    return str(tmp_path / "f0.sv")


def test_a_deep_acyclic_include_chain_is_followed(tmp_path):
    """The leaf of a long include chain is scanned."""
    top = _include_chain(
        tmp_path, 60, leaf_body="function int deep; return 1; endfunction\n"
    )
    findings = scan_files([str(top)])
    assert [f.name for f in findings] == ["deep"]
    assert findings[0].path.endswith("f60.svh")


def test_a_600_deep_include_chain_does_not_exhaust_the_python_stack(tmp_path):
    """A 600-deep include chain raises a structured fatal error, not RecursionError."""
    top = _include_chain(
        tmp_path, 600, leaf_body="function int deep; return 1; endfunction\n"
    )
    findings = scan_files([str(top)])
    assert [f.name for f in findings] == ["deep"]
    assert findings[0].path.endswith("f600.svh")


def test_the_advertised_include_depth_is_the_one_that_fires(tmp_path):
    """At `MAX_INCLUDE_DEPTH` the leaf is still scanned; one level deeper raises the structured fatal error."""
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools import sv_lifetime_scan

    depth = sv_lifetime_scan.MAX_INCLUDE_DEPTH
    at_cap = _include_chain(
        tmp_path / "at_cap",
        depth,
        leaf_body="function int deep; return 1; endfunction\n",
    )
    assert [f.name for f in scan_files([str(at_cap)])] == ["deep"]

    over = _include_chain(tmp_path / "over", depth + 1, leaf_body="// nothing\n")
    with pytest.raises(FatalRtlBuddyError, match="include nesting deeper than"):
        scan_files([str(over)])


def test_exceeding_the_include_depth_raises_instead_of_dropping(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools import sv_lifetime_scan

    monkey_depth = 4
    original = sv_lifetime_scan.MAX_INCLUDE_DEPTH
    sv_lifetime_scan.MAX_INCLUDE_DEPTH = monkey_depth
    try:
        top = _include_chain(
            tmp_path,
            monkey_depth + 3,
            leaf_body="function int deep; return 1; endfunction\n",
        )
        with pytest.raises(FatalRtlBuddyError, match="include nesting deeper than"):
            scan_files([str(top)])
    finally:
        sv_lifetime_scan.MAX_INCLUDE_DEPTH = original


def test_the_depth_error_names_the_chain(tmp_path):
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools import sv_lifetime_scan

    original = sv_lifetime_scan.MAX_INCLUDE_DEPTH
    sv_lifetime_scan.MAX_INCLUDE_DEPTH = 2
    try:
        top = _include_chain(tmp_path, 6, leaf_body="// nothing\n")
        with pytest.raises(FatalRtlBuddyError) as excinfo:
            scan_files([str(top)])
    finally:
        sv_lifetime_scan.MAX_INCLUDE_DEPTH = original
    assert ".svh" in str(excinfo.value)
    assert " -> " in str(excinfo.value)


def test_a_cycle_is_still_stopped_quietly(tmp_path):
    """An include cycle is not reported as a depth overflow."""
    (tmp_path / "a.svh").write_text('`include "b.svh"\n')
    (tmp_path / "b.svh").write_text(
        '`include "a.svh"\nfunction int f; return 1; endfunction\n'
    )
    top = tmp_path / "top.sv"
    top.write_text('module m;\n`include "a.svh"\nendmodule\n')
    assert [f.name for f in scan_files([str(top)])] == ["f"]


# Attribute instances are not qualifiers


def _attributed(attr):
    return dedent(f"""\
        module m;
          {attr} function int f(input int a); return a; endfunction
        endmodule
    """)


def test_escaped_identifier_in_an_attribute_does_not_exempt_the_function():
    """An escaped `\\extern` is a name, not the keyword, so the function body is reported."""
    assert _names(scan_text(_attributed(r"(* \extern = 1 *)"), "m.sv")) == [
        (2, "function", "f")
    ]


@pytest.mark.parametrize(
    "attr",
    [
        "(* keep *)",
        "(* extern *)",
        "(* pure *)",
        "(* virtual *)",
        "(* import *)",
        "(* typedef *)",
        '(* ram_style = "block" *)',
        "(* keep, dont_touch *)",
        "(* n = (1 + 2) *)",
        r"(* \extern *)",
        r"(* \automatic = 1 *)",
    ],
)
def test_an_attribute_never_changes_the_verdict(attr):
    assert _names(scan_text(_attributed(attr), "m.sv")) == [(2, "function", "f")]


def test_attribute_automatic_is_not_a_lifetime():
    """`(* automatic *)` is an attribute, not a lifetime keyword."""
    assert _names(scan_text(_attributed("(* automatic *)"), "m.sv")) == [
        (2, "function", "f")
    ]
    src = dedent("""\
        module m;
          (* keep *) function automatic int f(input int a); return a; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_a_real_extern_prototype_is_still_exempt():
    """The `extern` keyword still marks a prototype."""
    src = dedent("""\
        class C;
          extern function int g(input int a);
        endclass
        module m;
          function int f(input int a); return a; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["f"]


def test_an_attributed_declaration_does_not_corrupt_scope_tracking():
    """An `(* extern *)` attribute does not make the declaration a prototype, and its `endfunction` does not pop the enclosing module."""
    src = dedent("""\
        module automatic m;
          (* extern *) function int a(input int x); return x; endfunction
          function int b(input int x); return x; endfunction
        endmodule
        module n;
          function int c(input int x); return x; endfunction
        endmodule
    """)
    # Only c is a finding; a and b are inside `module automatic`.
    assert [f.name for f in scan_text(src, "m.sv")] == ["c"]


def test_an_attribute_on_a_module_header_is_ignored():
    src = dedent("""\
        (* keep_hierarchy *)
        module m;
          function int f(input int a); return a; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


def test_a_wildcard_sensitivity_list_is_not_mistaken_for_an_attribute():
    src = dedent("""\
        module m;
          always @(*) begin end
          function int f(input int a); return a; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


def test_an_unterminated_attribute_is_left_alone():
    """An unclosed `(*` does not swallow the rest of the file."""
    src = "module m;\n  (* keep\n  function int f(input int a); return a; endfunction\n"
    assert isinstance(scan_text(src, "m.sv"), list)


def test_a_parenthesised_expression_is_not_treated_as_an_attribute():
    src = dedent("""\
        module m;
          localparam int P = (4) * (2);
          function int f(input int a); return a; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


def test_an_escaped_identifier_named_like_a_qualifier_outside_an_attribute():
    src = dedent("""\
        module m;
          logic \\extern ;
          function int f(input int a); return a; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


# An escaped name is one identifier, separators and all


def test_an_escaped_name_containing_a_scope_separator_is_reported():
    r"""`\C::f` is a single escaped identifier, an ordinary subroutine, and is reported."""
    src = "module m;\n  function int \\C::f (input int a); return a; endfunction\nendmodule\n"
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "C::f")]


def test_an_escaped_name_containing_a_dot_is_reported():
    src = "module m;\n  function int \\a.b (input int a); return a; endfunction\nendmodule\n"
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "a.b")]


def test_an_escaped_task_name_with_a_separator_is_reported():
    src = "module m;\n  task \\i1.t2 (input int a); endtask\nendmodule\n"
    assert _names(scan_text(src, "m.sv")) == [(2, "task", "i1.t2")]


def test_a_real_out_of_block_definition_is_still_exempt():
    """An unescaped `::` marks an out-of-block class method."""
    src = dedent("""\
        module m;
          function int C::f(input int a); return a; endfunction
          task D::t(input int a); endtask
          function int plain; return 1; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["plain"]


def test_an_escaped_name_inside_a_class_is_still_exempt():
    src = "class C;\n  function int \\x::y (input int a); return a; endfunction\nendclass\n"
    assert scan_text(src, "m.sv") == []


def test_an_escaped_name_after_a_qualified_return_type_is_reported():
    r"""With return type `pkg::t_e` and name `\g::h`, the name does not inherit the type's `::`."""
    src = (
        "module m;\n"
        "  function pkg::t_e \\g::h (input int a); return 0; endfunction\n"
        "endmodule\n"
    )
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "g::h")]


def test_an_escaped_name_ending_in_a_separator_keeps_it():
    """A `::` that is part of an escaped name is not trimmed."""
    src = "module m;\n  function int \\f. (input int a); return a; endfunction\nendmodule\n"
    assert [f.name for f in scan_text(src, "m.sv")] == ["f."]


def test_an_escaped_name_with_no_separator_is_unaffected():
    src = "module m;\n  function int \\odd$name (input int a); return a; endfunction\nendmodule\n"
    assert [f.name for f in scan_text(src, "m.sv")] == ["odd$name"]


# Anonymous struct/union return types


def test_anonymous_packed_struct_return_type_on_an_out_of_block_method():
    """A `C::` method returning an anonymous struct is exempt; the struct's `;` does not end the header."""
    src = dedent("""\
        module m;
          function struct packed { logic a; } C::f(); return 0; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_anonymous_packed_union_return_type_on_an_out_of_block_method():
    src = dedent("""\
        module m;
          function union packed { logic a; logic b; } C::g(); return 0; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_anonymous_struct_return_type_on_a_module_scope_function():
    """A module-scope function returning an anonymous struct is reported under its own name."""
    src = dedent("""\
        module m;
          function struct packed { logic a; } free_fn(); return 0; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "free_fn")]


def test_a_nested_anonymous_struct_return_type():
    src = dedent("""\
        module m;
          function struct packed {
            struct packed { logic x; } inner;
            logic y;
          } nested_fn(); return 0; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["nested_fn"]


def test_an_anonymous_struct_return_type_with_packed_ranges():
    src = dedent("""\
        module m;
          function struct packed { logic [7:0] a; logic [3:0] b; } ranged();
            return 0;
          endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["ranged"]


def test_an_anonymous_struct_return_type_with_no_argument_list():
    """The header ends at its own `;`, not the struct's."""
    src = dedent("""\
        module m;
          function struct packed { logic a; } noargs; return 0; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "noargs")]


def test_automatic_still_exempts_a_struct_returning_function():
    src = dedent("""\
        module m;
          function automatic struct packed { logic a; } ok_fn(); return 0; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_a_struct_returning_task_free_function_is_still_found_after_one():
    """Declarations after an anonymous struct return type are scanned normally."""
    src = dedent("""\
        module m;
          function struct packed { logic a; } first(); return 0; endfunction
          function int second(input int x); return x; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["first", "second"]


def test_an_unclosed_struct_brace_does_not_hang_the_scan():
    src = "module m;\n  function struct packed { logic a; f();\n"
    assert isinstance(scan_text(src, "m.sv"), list)


def test_a_parameterised_and_struct_returning_out_of_block_method():
    src = dedent("""\
        module m;
          function struct packed { logic a; } D#(int)::h(); return 0; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


# Parenthesised return types


def test_type_reference_return_type_on_an_out_of_block_method_is_exempt():
    """`type(expr)` is a type reference (LRM 6.23), so a `C::` method returning it is exempt."""
    src = dedent("""\
        module m;
          function type(int) C::f(); return 0; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_type_reference_return_type_on_a_module_scope_function_is_named_right():
    src = dedent("""\
        module m;
          function type(x) g(); return 0; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "g")]


def test_type_reference_return_type_with_no_argument_list():
    src = dedent("""\
        module m;
          function type(x) noargs; return 0; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "noargs")]


def test_automatic_still_exempts_a_type_reference_returning_function():
    src = dedent("""\
        module m;
          function automatic type(x) g(); return 0; endfunction
        endmodule
    """)
    assert scan_text(src, "m.sv") == []


def test_a_type_reference_containing_a_call_is_skipped_whole():
    src = dedent("""\
        module m;
          function type(a + b(1)) g(); return 0; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["g"]


def test_a_type_reference_does_not_desynchronise_later_declarations():
    src = dedent("""\
        module m;
          function type(x) first(); return 0; endfunction
          function int second(input int a); return a; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["first", "second"]


def test_an_unclosed_type_reference_does_not_hang_the_scan():
    src = "module m;\n  function type(x g(); return 0; endfunction\n"
    assert isinstance(scan_text(src, "m.sv"), list)


def test_an_escaped_identifier_named_type_is_not_a_type_reference():
    r"""An escaped `\type` is a name, so the following `(` opens the argument list."""
    src = "module m;\n  function int \\type (input int a); return a; endfunction\nendmodule\n"
    assert [f.name for f in scan_text(src, "m.sv")] == ["type"]


def test_other_legal_parenthesised_return_type_shapes_are_already_handled():
    """An anonymous enum body and a packed dimension containing a call do not confuse the scan."""
    enum_src = dedent("""\
        module m;
          function enum { A, B } from_enum(); return A; endfunction
        endmodule
    """)
    dim_src = dedent("""\
        module m;
          function bit [$clog2(W)-1:0] from_dim(); return 0; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(enum_src, "m.sv")] == ["from_enum"]
    assert [f.name for f in scan_text(dim_src, "m.sv")] == ["from_dim"]


# A class method may not have a static lifetime


@pytest.mark.parametrize(
    "src",
    [
        "class C;\n  function static int f(input int a); return a; endfunction\nendclass\n",
        "class C;\n  task static run(input int a); endtask\nendclass\n",
        "class C;\n  virtual function static int f(input int a); return a; endfunction\nendclass\n",
        "module m;\n  function static int C::f(input int a); return a; endfunction\nendmodule\n",
        "module m;\n  task static C::run(input int a); endtask\nendmodule\n",
    ],
)
def test_a_static_lifetime_on_a_class_method_is_not_reported(src):
    """`function static` on a class method is not reported; slang rejects it at parse time."""
    assert scan_text(src, "m.sv") == []


def test_a_class_static_method_qualifier_is_still_exempt():
    """`static function` declares a class-static method and is not a lifetime."""
    src = dedent("""\
        class C;
          static function int g(input int a); return a; endfunction
        endclass
    """)
    assert scan_text(src, "m.sv") == []


def test_an_explicit_static_lifetime_outside_a_class_is_still_reported():
    """`static` on a non-class function is still reported."""
    src = dedent("""\
        module m;
          function static int f(input int a); return a; endfunction
          task static run(input int a); endtask
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["f", "run"]


# Nested subroutines do not exist in SystemVerilog

# A subroutine body cannot contain a `function` or `task` declaration (LRM 1800-2017 A.2.7/A.2.8).
# These tests pin that the scan is stable on such input; they assert no verdict on it.

_NESTED_IN_CLASS = dedent("""\
    class C;
      function int outer(input int a);
        function static int inner(input int b); return b; endfunction
        return inner(a);
      endfunction
    endclass
""")

_NESTED_OUT_OF_BLOCK = dedent("""\
    module m;
      function int C::outer(input int a);
        function static int inner(input int b); return b; endfunction
        return inner(a);
      endfunction
    endmodule
""")

_NESTED_IN_MODULE = dedent("""\
    module m;
      function automatic int outer(input int a);
        function static int inner(input int b); return b; endfunction
        return inner(a);
      endfunction
    endmodule
""")

_NESTED_TASK_IN_CLASS = dedent("""\
    class C;
      task outer();
        task static inner(); endtask
      endtask
    endclass
""")


@pytest.mark.parametrize(
    "src, expected",
    [
        (_NESTED_IN_CLASS, []),
        (_NESTED_TASK_IN_CLASS, []),
        # An out-of-block definition pushes a subroutine scope, not a class scope.
        (_NESTED_OUT_OF_BLOCK, ["inner"]),
        (_NESTED_IN_MODULE, ["inner"]),
    ],
)
def test_nested_subroutine_shapes_are_stable(src, expected):
    assert [f.name for f in scan_text(src, "m.sv")] == expected


def test_a_class_method_with_a_legal_body_declaration_is_still_exempt():
    """Data, parameter, type and `let` declarations in a subroutine body are not subroutines."""
    src = dedent("""\
        class C;
          function int outer(input int a);
            int scratch;
            localparam int P = 4;
            typedef bit [3:0] nib_t;
            let double(v) = v * 2;
            return double(a) + P + scratch;
          endfunction
        endclass
    """)
    assert scan_text(src, "m.sv") == []


def test_a_module_function_with_a_legal_body_declaration_is_still_reported():
    src = dedent("""\
        module m;
          function int outer(input int a);
            int scratch;
            localparam int P = 4;
            return a + P + scratch;
          endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "outer")]


def test_a_class_method_does_not_leak_its_exemption_to_a_later_module():
    """The class-method exemption ends with the class scope."""
    src = dedent("""\
        class C;
          function int inside(input int a); return a; endfunction
        endclass
        module m;
          function int outside(input int a); return a; endfunction
        endmodule
    """)
    assert [f.name for f in scan_text(src, "m.sv")] == ["outside"]
