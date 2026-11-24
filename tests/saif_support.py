"""A one-buffer technology and a `read_saif` session in real OpenROAD, for SAIF name tests.

The LEF and Liberty are written inline, so the tests need OpenROAD on PATH and no PDK.
"""

import shutil
import subprocess
from textwrap import dedent

OPENROAD = shutil.which("openroad")

_LEF = dedent("""\
    VERSION 5.8 ;
    BUSBITCHARS "[]" ;
    DIVIDERCHAR "/" ;
    UNITS
      DATABASE MICRONS 1000 ;
    END UNITS
    SITE core
      CLASS CORE ;
      SIZE 0.2 BY 2.0 ;
    END core
    LAYER met1
      TYPE ROUTING ; DIRECTION HORIZONTAL ; PITCH 0.2 ; WIDTH 0.1 ; SPACING 0.1 ;
    END met1
    MACRO BUF
      CLASS CORE ;
      ORIGIN 0 0 ;
      SIZE 0.4 BY 2.0 ;
      SITE core ;
      PIN A
        DIRECTION INPUT ;
        PORT
          LAYER met1 ; RECT 0.0 0.0 0.1 0.1 ;
        END
      END A
      PIN Y
        DIRECTION OUTPUT ;
        PORT
          LAYER met1 ; RECT 0.3 0.0 0.4 0.1 ;
        END
      END Y
    END BUF
    END LIBRARY
    """)

_LIB = dedent("""\
    library (rb_saif_test) {
      time_unit : "1ns" ;
      voltage_unit : "1V" ;
      current_unit : "1mA" ;
      capacitive_load_unit (1, pf) ;
      leakage_power_unit : "1nW" ;
      nom_voltage : 1.0 ;
      cell (BUF) {
        area : 1.0 ;
        pin (A) { direction : input ; capacitance : 0.001 ; }
        pin (Y) {
          direction : output ;
          function : "A" ;
          timing () {
            related_pin : "A" ;
            timing_sense : positive_unate ;
            cell_rise (scalar) { values ("0.01") ; }
            cell_fall (scalar) { values ("0.01") ; }
            rise_transition (scalar) { values ("0.01") ; }
            fall_transition (scalar) { values ("0.01") ; }
          }
        }
      }
    }
    """)

# Two buffers on a net with an escaped name, the first instance with one too: what a
# flattened gate-level netlist holds.
NETLIST = dedent("""\
    module top (input clk, output y);
      wire \\u_a.n1 ;
      BUF \\u_a.u0 (.A(clk), .Y(\\u_a.n1 ));
      BUF u1 (.A(\\u_a.n1 ), .Y(y));
    endmodule
    """)


def read_saif(tmp_path, saif) -> str:
    """Link `NETLIST`, `read_saif` the file, and return OpenROAD's output.

    The output holds `Annotated <n> pin activities.` on success, `result: <error id>` when
    the read fails, and the annotated pins, one ` saif <pin>` line each.
    """
    (tmp_path / "cells.lef").write_text(_LEF)
    (tmp_path / "cells.lib").write_text(_LIB)
    (tmp_path / "top.v").write_text(NETLIST)
    script = tmp_path / "read.tcl"
    script.write_text(
        dedent(f"""\
            read_lef cells.lef
            read_liberty cells.lib
            read_verilog top.v
            link_design top
            create_clock -name clk -period 1 [get_ports clk]
            catch {{read_saif -scope top {saif}}} err
            puts "result: $err"
            report_activity_annotation -report_annotated
            """)
    )
    proc = subprocess.run(
        [OPENROAD, "-no_init", "-exit", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return proc.stdout + proc.stderr
