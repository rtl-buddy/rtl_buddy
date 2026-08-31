"""Tests for the pre-synthesis static-lifetime scan (rtl-buddy/rtl_buddy#472)."""

from textwrap import dedent

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
