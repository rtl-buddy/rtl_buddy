"""Vivado non-project batch-Tcl flow template for ``rb fpga``.

:func:`render_flow_tcl` renders :data:`FLOW_TCL_TEMPLATE`, run as
``vivado -mode batch -source flow.tcl -nojournal -log <log>``. Stages: read sources and XDC,
``synth_design``, ``opt_design``, ``place_design``, ``route_design``, reports, ``write_bitstream``.
:data:`FLOW_STAGES` and :data:`REPORT_FILES` are shared with the backend, the report
parsers (:mod:`.fpga_vivado_reports`) and the tests. Placeholders use ``{{ key }}``.
"""

from __future__ import annotations

import re

# (stage_name, tcl_command) in execution order.
FLOW_STAGES: tuple[tuple[str, str], ...] = (
    ("synth", "synth_design -top {{ top }} -part {{ part }}{{ include_dirs }}"),
    ("opt", "opt_design"),
    ("place", "place_design"),
    ("route", "route_design"),
)

# Post-route reports: key -> filename under ``artefacts/<run>/``. Keys match the
# ``parse_<key>`` functions in fpga_vivado_reports.
REPORT_FILES: dict[str, str] = {
    "utilization": "util.rpt",
    "timing_summary": "timing_summary.rpt",
    "power": "power.rpt",
    "drc": "drc.rpt",
    "methodology": "methodology.rpt",
}

# Tcl command emitted for each report key.
_REPORT_TCL: dict[str, str] = {
    "utilization": "report_utilization -file {file}",
    "timing_summary": "report_timing_summary -file {file}",
    "power": "report_power -file {file}",
    "drc": "report_drc -file {file}",
    "methodology": "report_methodology -file {file}",
}


def report_tcl_commands(report_files: dict[str, str] | None = None) -> list[str]:
    """Render the ``report_*`` commands for a report-file mapping.

    Raises:
      RuntimeError: a key is not a known report name.
    """
    files = REPORT_FILES if report_files is None else report_files
    commands: list[str] = []
    for key, filename in files.items():
        if key not in _REPORT_TCL:
            raise RuntimeError(f"fpga flow: unknown report '{key}'")
        commands.append(_REPORT_TCL[key].format(file=filename))
    return commands


def _build_template() -> str:
    """Assemble the flow template from the stage and report tables."""
    lines: list[str] = [
        "# Vivado non-project batch flow -- templated by rb fpga.",
        "#",
        "# Placeholders are substituted by Python before invoking",
        "#   vivado -mode batch -source flow.tcl -nojournal -log <log>",
        "# Stage order: read sources/XDC -> synth -> opt -> place -> route",
        "# -> reports -> bitstream.",
        "",
        'puts ">>> Reading sources"',
        "{{ read_sources }}",
        "",
        'puts ">>> Reading constraints"',
        "{{ read_constraints }}",
        "",
    ]
    for stage, command in FLOW_STAGES:
        lines.append(f'puts ">>> Stage: {stage}"')
        lines.append(command)
        lines.append("")
    lines.append('puts ">>> Reports"')
    lines.append("{{ reports }}")
    lines.append("")
    lines.append('puts ">>> Bitstream"')
    lines.append("{{ bitstream_cmd }}")
    lines.append("")
    lines.append('puts ">>> DONE"')
    lines.append("")
    return "\n".join(lines)


FLOW_TCL_TEMPLATE: str = _build_template()


def _read_source_commands(verilog_sources: list[str]) -> list[str]:
    """One read command per source: ``read_verilog -sv`` for ``.sv``, ``read_vhdl`` for ``.vhd``/``.vhdl``, else ``read_verilog``."""
    commands: list[str] = []
    for src in verilog_sources:
        lower = src.lower()
        if lower.endswith(".sv"):
            commands.append(f"read_verilog -sv {src}")
        elif lower.endswith((".vhd", ".vhdl")):
            commands.append(f"read_vhdl {src}")
        else:
            commands.append(f"read_verilog {src}")
    return commands


def tcl_string(value: str) -> str:
    """Return ``value`` as a double-quoted Tcl word with backslash, quote, ``$`` and brackets escaped."""
    escaped = re.sub(r'([\\"$\[\]])', r"\\\1", value)
    return f'"{escaped}"'


def include_dirs_arg(include_dirs: list[str]) -> str:
    """Return ``synth_design``'s ``-include_dirs`` option for the filelist's ``+incdir+`` directories, or ``""``.

    Paths are quoted with :func:`tcl_string`, so spaces and Tcl metacharacters are safe.
    """
    if not include_dirs:
        return ""
    return " -include_dirs [list " + " ".join(tcl_string(d) for d in include_dirs) + "]"


def render_flow_tcl(
    *,
    top: str,
    part: str,
    verilog_sources: list[str],
    xdc_files: list[str],
    include_dirs: list[str] | None = None,
    bitstream: str | None = None,
    emit_bitstream: bool = True,
    report_files: dict[str, str] | None = None,
) -> str:
    """Render the batch-Tcl flow script for one ``rb fpga`` run.

    Args:
      top: Top module name passed to ``synth_design -top``.
      part: Full Vivado part name (e.g. ``xczu7ev-ffvc1156-2-e``).
      verilog_sources: HDL sources, read in order.
      xdc_files: Constraint files, read in order.
      include_dirs: `` `include `` search directories, passed to
        ``synth_design -include_dirs``.
      bitstream: Output bitstream filename. Defaults to ``<top>.bit``.
      emit_bitstream: When False, the ``write_bitstream`` stage is
        replaced with a comment.
      report_files: Override the default report-file mapping
        (:data:`REPORT_FILES`); keys must be known report names.

    Raises:
      RuntimeError: on missing inputs or unsubstituted placeholders.
    """
    if not top:
        raise RuntimeError("fpga flow: top module name is required")
    if not part:
        raise RuntimeError("fpga flow: part name is required")
    if not verilog_sources:
        raise RuntimeError("fpga flow: at least one HDL source is required")

    read_constraints = "\n".join(f"read_xdc {xdc}" for xdc in xdc_files)
    if not xdc_files:
        read_constraints = "# (no XDC constraints provided)"

    if emit_bitstream:
        bitstream_cmd = "\n".join(
            [
                # IP-level models lack pin constraints, which bitgen reports as
                # NSTD-1/UCIO-1 errors; report_drc already recorded them.
                "set_property SEVERITY {Warning} [get_drc_checks NSTD-1]",
                "set_property SEVERITY {Warning} [get_drc_checks UCIO-1]",
                f"write_bitstream -force {bitstream or f'{top}.bit'}",
            ]
        )
    else:
        bitstream_cmd = "# (bitstream generation not requested)"

    substitutions = {
        "top": top,
        "part": part,
        "read_sources": "\n".join(_read_source_commands(verilog_sources)),
        "read_constraints": read_constraints,
        "include_dirs": include_dirs_arg(include_dirs or []),
        "reports": "\n".join(report_tcl_commands(report_files)),
        "bitstream_cmd": bitstream_cmd,
    }

    script = FLOW_TCL_TEMPLATE
    for key, value in substitutions.items():
        script = script.replace("{{ " + key + " }}", str(value))

    leftover = re.findall(r"\{\{\s*[\w]+\s*\}\}", script)
    if leftover:
        raise RuntimeError(
            f"fpga flow template has unsubstituted placeholders: {leftover}"
        )
    return script
