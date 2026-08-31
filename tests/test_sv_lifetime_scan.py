"""Tests for the pre-synthesis static-lifetime scan (rtl-buddy/rtl_buddy#472)."""

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


# ---------------------------------------------------------------------------
# The issue's repro
# ---------------------------------------------------------------------------

# Verbatim from rtl-buddy/rtl_buddy#472. `inc` is on line 9 and `same` on
# line 10 of the module below; both lack `automatic`, so yosys-slang gives
# each one shared net per formal and aliases the two call sites.
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


# ---------------------------------------------------------------------------
# Lifetime resolution
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Exemptions
# ---------------------------------------------------------------------------


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
    # Only the real declaration on line 6 is reported.
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


# ---------------------------------------------------------------------------
# Tokenizer robustness
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# `include following (review item 2)
# ---------------------------------------------------------------------------


def test_include_relative_to_the_including_file_is_scanned(tmp_path):
    """The issue's repro split into a header: the declarations still count."""
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
    # Reported against the header, not the includer.
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
    # The rest of the file is still scanned, and the miss is not fatal.
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


# ---------------------------------------------------------------------------
# Conditional compilation (review item 3)
# ---------------------------------------------------------------------------


def test_ifndef_region_excluded_by_a_run_define_is_not_reported():
    """The reviewer's case: a sim-only helper behind `ifndef SYNTHESIS."""
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
    # Without the define the helper is compiled, so it is reported.
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


def test_defines_carry_from_one_source_to_the_next(tmp_path):
    (tmp_path / "a.sv").write_text("`define SHARED 1\n")
    (tmp_path / "b.sv").write_text(
        "module b;\n`ifndef SHARED\n"
        "  function int hidden; return 1; endfunction\n`endif\nendmodule\n"
    )
    assert scan_files([str(tmp_path / "a.sv"), str(tmp_path / "b.sv")]) == []


# ---------------------------------------------------------------------------
# Macro bodies (review item 7)
# ---------------------------------------------------------------------------


def test_declaration_inside_a_define_body_is_not_reported():
    """A macro body is scanned where it expands, and rtl_buddy does not expand
    macros — so it must not be reported at the `define either."""
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


def test_escaped_identifier_is_never_read_as_a_keyword():
    src = dedent("""\
        module m;
          import "DPI-C" function void \\begin (input int a);
          function int f; return 1; endfunction
        endmodule
    """)
    # The escaped \\begin must not clear the pending `import "DPI-C"` window,
    # or the DPI prototype would be reported as a declaration.
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


def test_escaped_identifier_named_like_a_scope_keyword_is_inert():
    src = dedent("""\
        module m;
          wire \\endmodule ;
          function int f; return 1; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(3, "function", "f")]


# ---------------------------------------------------------------------------
# Out-of-body class method definitions (review item 6)
# ---------------------------------------------------------------------------


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
    """`pkg::t_e` is the return type, not a qualified name — the function is
    an ordinary module-scope declaration and must not be exempted."""
    src = dedent("""\
        module m;
          function pkg::state_e decode(input int a); return 0; endfunction
        endmodule
    """)
    assert _names(scan_text(src, "m.sv")) == [(2, "function", "decode")]
