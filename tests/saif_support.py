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


def random_vcd(seed: int, signals: int = 40, steps: int = 60) -> str:
    """A deterministic VCD of 1- to 33-bit signals with 0/1/x/z values, aliases and a nested scope.

    For checking `rb saif`'s one-pass read against a per-variable scan of the same trace.
    """
    import random

    rng = random.Random(seed)
    lines = ["$timescale 1ns $end", "$scope module top $end"]
    sigs = []
    for i in range(signals):
        width = rng.choice([1, 1, 1, 2, 5, 8, 33])
        code = f"s{i}"
        lines.append(f"$var wire {width} {code} v{i} $end")
        sigs.append((code, width))
        if i % 7 == 3:
            # A second var on the same signal, as a port and its net are.
            lines.append(f"$var wire {width} {code} alias{i} $end")
    lines += ["$scope module sub $end", "$var wire 1 s0 clk_copy $end", "$upscope $end"]
    lines += ["$upscope $end", "$enddefinitions $end"]
    t = 0
    for step in range(steps):
        t += rng.choice([1, 3, 7])
        lines.append(f"#{t}")
        for code, width in sigs:
            if step and rng.random() >= 0.3:
                continue
            if width == 1:
                lines.append(rng.choice("01xz01") + code)
            else:
                value = "".join(rng.choice("0101010xz") for _ in range(width))
                if rng.random() < 0.3:
                    # A binary value, which pywellen hands over as an int.
                    value = value.replace("x", "0").replace("z", "1")
                lines.append(f"b{value} {code}")
    return "\n".join(lines) + "\n"
