"""Vivado non-project batch-Tcl flow template for ``rb fpga``.

This module pins down the Tcl contract between rtl_buddy and Vivado
before any invocation code exists (issue #284). It deliberately contains
no subprocess code — the P1 backend renders :data:`FLOW_TCL_TEMPLATE`
via :func:`render_flow_tcl` and drives::

    vivado -mode batch -source flow.tcl -nojournal -log <log>

Stage order (non-project mode): read sources + XDC -> ``synth_design``
-> ``opt_design`` -> ``place_design`` -> ``route_design`` -> post-route
reports -> ``write_bitstream``.

The stage list (:data:`FLOW_STAGES`) and report set
(:data:`REPORT_FILES`) are module data so the backend, the report
parsers (:mod:`.fpga_vivado_reports`), and the tests all agree on one
contract. The template uses the same ``{{ key }}`` placeholder style as
``rtl_buddy/pnr/flow.tcl.template``.
"""

from __future__ import annotations

import re

# Implementation stages in execution order. Each entry is
# (stage_name, tcl_command). Stage names are stable identifiers the P1
# runner can use for progress reporting; the synth command carries the
# ``{{ top }}`` / ``{{ part }}`` placeholders resolved at render time.
FLOW_STAGES: tuple[tuple[str, str], ...] = (
    ("synth", "synth_design -top {{ top }} -part {{ part }}"),
    ("opt", "opt_design"),
    ("place", "place_design"),
    ("route", "route_design"),
)

# Post-route reports: report key -> output filename (relative to the
# Vivado cwd, i.e. ``artefacts/<run>/``). Keys match the parser names in
# fpga_vivado_reports (parse_<key>) and the fixture files under
# ``tests/fixtures/fpga/``.
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
    """Render the ``report_*`` command block from a report-file mapping.

    Unknown report keys raise so a typo'd report name fails at template
    time rather than producing a silent gap in the artefacts.
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
    """Emit one read command per source file.

    ``.sv`` sources get ``read_verilog -sv``; ``.vhd``/``.vhdl`` get
    ``read_vhdl``; everything else is plain ``read_verilog``.
    """
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


def render_flow_tcl(
    *,
    top: str,
    part: str,
    verilog_sources: list[str],
    xdc_files: list[str],
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
      bitstream: Output bitstream filename. Defaults to ``<top>.bit``.
      emit_bitstream: When False, the ``write_bitstream`` stage is
        replaced with a comment (smoke/timing runs don't need bitgen).
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
                # write_bitstream's precondition DRC escalates NSTD-1 /
                # UCIO-1 (no IOSTANDARD / no pin LOC) to errors. rb fpga
                # targets IP-level models that usually have no board
                # pinout, so downgrade the two checks to warnings just
                # for bitgen — report_drc above already ran and records
                # them at their original severity. Board projects that
                # constrain every pin are unaffected.
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
        "reports": "\n".join(report_tcl_commands(report_files)),
        "bitstream_cmd": bitstream_cmd,
    }

    script = FLOW_TCL_TEMPLATE
    for key, value in substitutions.items():
        script = script.replace("{{ " + key + " }}", str(value))

    # Surface any unsubstituted placeholders early (same guard as the
    # pnr flow template).
    leftover = re.findall(r"\{\{\s*[\w]+\s*\}\}", script)
    if leftover:
        raise RuntimeError(
            f"fpga flow template has unsubstituted placeholders: {leftover}"
        )
    return script
