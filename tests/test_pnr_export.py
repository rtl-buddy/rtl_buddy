"""Tests for `rb pnr-export` — exporting a saved P&R result (#618).

What these pin:

* the acceptance test of the issue: with synthesis and the OpenROAD launch
  point rigged to raise, an export still produces GDS and PNG from a saved
  routed DEF;
* the up-front validation — DEF present, non-empty, and *this design's* —
  and that each failure stops before KLayout;
* that an export never touches the routed DEF, ODB, netlist or SDC, on the
  failing path as much as the passing one;
* the exit-code contract: in export-only the export is the job, so a
  failure is a FAIL in both modes, while a published-but-incomplete
  `preview` layout stays a qualified pass;
* the PNG-only re-render, its `--lyp` / resolution overrides on the KLayout
  command line, and the qualifier it inherits from the report beside the
  GDS;
* the export record, and that no failure leaves a stale successful output.

Backend log assertions go through `_capture_pnr_events` rather than
caplog: `task_status` initialises rtl_buddy's own logging on the first
console write, which clears the handlers pytest installed (#619). Console
assertions read `result.output` for the same reason.
"""

import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import PnrConfig

_FIXTURES = Path(__file__).parent / "fixtures"

_GDS_BYTES = b"\x00\x06\x00\x02\x00\x07"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n"

#: What the routed result on disk holds. Bytes, so a test can assert the
#: export left every one of them alone.
_ROUTED = {
    "{design}.def": "DESIGN {design} ;\nEND DESIGN\n",
    "{design}.routed.v": "module {design}(); endmodule\n",
    "{design}.routed.sdc": "create_clock -period 1 [get_ports clk]\n",
    "{design}.routed.odb": "odb-bytes-for-{design}\n",
}


def _touch(*paths):
    """Materialize configured input files as empty placeholders."""
    for path in paths:
        if not path:
            continue
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


def _make_stream_pdk(tmp_path, **overrides):
    """A PDK whose stream-out inputs all exist on disk.

    An input the config names and the disk does not have stops the export
    before KLayout is launched (#617), so a test that wants to reach the
    tool has to put those files there.
    """
    base = dict(
        name="nangate45",
        site="FreePDK45_38x28_10R_NP_162NW_34O",
        corners={"typ": "pdk/lib/typ.lib"},
        tech_lef="pdk/lef/tech.lef",
        macro_lef="pdk/lef/cells.lef",
        klayout_tech="pdk/klayout/tech.lyt",
        klayout_props="pdk/klayout/props.lyp",
        cell_gds="pdk/gds/cells.gds",
    )
    base.update(overrides)
    pdk = PdkConfig(PdkConfigFile(**base), str(tmp_path / "root_config.yaml"))
    _touch(
        pdk.get_klayout_tech(),
        pdk.get_klayout_props(),
        pdk.get_tech_lef(),
        pdk.get_macro_lef(),
        *pdk.get_cell_gds_paths(),
    )
    return pdk


def _make_pnr_cfg(tmp_path, **overrides):
    from rtl_buddy.config.pnr import PnrFloorplan

    base = dict(
        name="demo_pnr",
        desc="demo",
        tool="openroad",
        synth_name="demo_synth",
        synth_suite_path=str(tmp_path / "synth.yaml"),
        constraints=str(tmp_path / "constraints.sdc"),
        platform="nangate45_typ",
        floorplan=PnrFloorplan(utilization=0.55, aspect=1.0, core_margin=2.0),
        _reglvl=1000,
        tool_overrides=None,
    )
    base.update(overrides)
    return PnrConfig(**base)


def _capture_pnr_events(monkeypatch):
    """Record every `log_event` the P&R backend emits, with its level."""
    from rtl_buddy.tools import pnr_openroad

    events: list[tuple[int, str, dict]] = []
    real = pnr_openroad.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _record)
    return events


def _one_event(events, name):
    """The single recorded event called `name`, as `(level, fields)`."""
    matches = [(level, fields) for level, event, fields in events if event == name]
    assert len(matches) == 1, f"{name}: {[e[1] for e in events]}"
    return matches[0]


def _write_synth_config(tmp_path, design="demo_top"):
    """The upstream synth *entry* an export reads its design name from.

    Deliberately without any synth artefact: the point of #618 is that the
    export needs the configuration and none of the products.
    """
    (tmp_path / "models.yaml").write_text(
        dedent(f"""\
        rtl-buddy-filetype: model_config
        models:
          - name: "{design}"
            filelist: []
        """)
    )
    (tmp_path / "synth.yaml").write_text(
        dedent(f"""\
        rtl-buddy-filetype: synth_config
        syntheses:
          - name: "demo_synth"
            desc: "demo"
            model: "{design}"
            model_path: "models.yaml"
            tool: "openroad"
            reglvl: 0
        """)
    )


def _fake_klayout(
    backend,
    design,
    *,
    missing=(),
    allowed_empty=(),
    orphans=(),
    gds=_GDS_BYTES,
    png=_PNG_BYTES,
    report=True,
    returncode=None,
    seen=None,
):
    """A `subprocess.run` stand-in for both bundled KLayout helpers.

    The stream-out writes the GDS and then the JSON report that says which
    cells came out empty, exiting with the helper's error count; the render
    writes the PNG. Every command line is appended to `seen`, which is how
    the `--lyp` and resolution overrides are checked.
    """
    artefacts = Path(backend.artefact_dir)
    out_gds = artefacts / f"{design}.gds"
    out_png = artefacts / f"{design}.png"
    report_path = artefacts / "def2stream.report.json"

    def _run(cmd, **_kwargs):
        cmd = list(cmd)
        if seen is not None:
            seen.append(cmd)
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        if cmd[1:] == ["-v"]:
            result.returncode = 0
            result.stdout = "KLayout 0.30.8\n"
            return result
        if any(arg.startswith("out_png=") for arg in cmd):
            if png is not None:
                out_png.write_bytes(png)
            result.returncode = 0 if png is not None else 1
            return result
        errors = len(missing) + len(orphans)
        if gds is not None:
            out_gds.write_bytes(gds)
        if report is True:
            report_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "design": design,
                        "out_file": str(out_gds),
                        "complete": not missing,
                        "missing_cells": list(missing),
                        "allowed_empty_cells": list(allowed_empty),
                        "orphan_cells": list(orphans),
                        "other_errors": len(orphans),
                        "errors": errors,
                    }
                )
            )
        elif isinstance(report, str):
            report_path.write_text(report)
        result.returncode = errors if returncode is None else returncode
        return result

    return _run


def _export_backend(
    tmp_path,
    monkeypatch,
    *,
    design="demo_top",
    mode=None,
    emit_png=False,
    klayout="/opt/klayout",
    pdk=None,
    routed=True,
    **backend_kwargs,
):
    """An `OpenRoadPnr` over a saved routed result, with no tools on PATH.

    `subprocess.run` is left unpatched on purpose — a test that wants
    KLayout patches it with `_fake_klayout`, and one that does not would
    rather a stray launch blow up than succeed quietly.
    """
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    _write_synth_config(tmp_path, design)
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: klayout)

    pdk = pdk if pdk is not None else _make_stream_pdk(tmp_path)
    platform = MagicMock()
    platform.get_pdk.return_value = pdk
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform

    backend = OpenRoadPnr(
        name="demo/export",
        pnr_cfg=_make_pnr_cfg(tmp_path, gds_mode=mode)
        if mode
        else _make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
        emit_gds=True,
        emit_png=emit_png,
        gds_mode=mode,
        **backend_kwargs,
    )
    artefacts = Path(backend.artefact_dir)
    if routed:
        for name, text in _ROUTED.items():
            (artefacts / name.format(design=design)).write_text(
                text.format(design=design)
            )
    return backend, artefacts


def _routed_digests(artefacts, design="demo_top"):
    """SHA-256 of every routed artefact, so an export can be shown to have
    left the P&R result exactly as it found it."""
    return {
        name: hashlib.sha256(
            (artefacts / name.format(design=design)).read_bytes()
        ).hexdigest()
        for name in _ROUTED
    }


# ---------------------------------------------------------------------------
# The acceptance test: an export launches neither synthesis nor OpenROAD
# ---------------------------------------------------------------------------


def test_export_only_produces_a_layout_without_synthesis_or_openroad(
    tmp_path, monkeypatch
):
    """The issue's acceptance criterion. Every path that would launch a
    synthesis or a P&R is rigged to raise, and the export still streams the
    saved DEF out and renders it (#618)."""
    from rtl_buddy.runner.synth_runner import SynthRunner
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("export-only launched a P&R or a synthesis")

    monkeypatch.setattr(OpenRoadPnr, "run", _must_not_run)
    monkeypatch.setattr(OpenRoadPnr, "_write_script", _must_not_run)
    monkeypatch.setattr(OpenRoadPnr, "_resolve_netlist_path", _must_not_run)
    monkeypatch.setattr(OpenRoadPnr, "_probe_openroad_version", _must_not_run)
    monkeypatch.setattr(SynthRunner, "run", _must_not_run)

    backend, artefacts = _export_backend(tmp_path, monkeypatch, emit_png=True)
    seen: list[list[str]] = []
    launched = _fake_klayout(backend, "demo_top", seen=seen)

    def _run(cmd, **kwargs):
        exe = os.path.basename(str(cmd[0]))
        if exe in {"openroad", "yosys"}:
            raise AssertionError(f"export-only launched {exe}: {cmd}")
        return launched(cmd, **kwargs)

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)

    res = backend.export_only()

    assert res.results["result"] == "PASS"
    assert res.results["gds_status"] == "complete"
    assert (artefacts / "demo_top.gds").read_bytes() == _GDS_BYTES
    assert (artefacts / "demo_top.png").read_bytes() == _PNG_BYTES
    assert res.results["gds_path"] == str(artefacts / "demo_top.gds")
    assert res.results["png_path"] == str(artefacts / "demo_top.png")
    # Only KLayout was ever launched (the version probe included).
    assert {os.path.basename(str(cmd[0])) for cmd in seen} == {"klayout"}


def test_export_only_leaves_every_routed_artefact_byte_identical(tmp_path, monkeypatch):
    """`run` starts by clearing the DEF, ODB, netlist and SDC; an export
    over a saved result must clear only what it publishes (#618)."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, emit_png=True)
    before = _routed_digests(artefacts)
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top")
    )

    assert backend.export_only().results["result"] == "PASS"

    assert _routed_digests(artefacts) == before


def test_a_failed_strict_export_still_leaves_the_routed_result_alone(
    tmp_path, monkeypatch
):
    """The failing path is the one that matters: a strict export that
    publishes nothing withdraws its own layout and nothing else."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, mode="strict")
    before = _routed_digests(artefacts)
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", missing=["sram_32x64"]),
    )

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "export"
    assert not (artefacts / "demo_top.gds").exists()
    assert not (artefacts / "def2stream.report.json").exists()
    assert _routed_digests(artefacts) == before


# ---------------------------------------------------------------------------
# Up-front validation: nothing is launched over a result that is not there
# ---------------------------------------------------------------------------


def _refuse_every_subprocess(monkeypatch):
    """Fail the test if anything but the KLayout version probe is launched.

    The probe is the readiness check every export makes up front and reads
    nothing; a stream-out, a render, OpenROAD or yosys over a result that
    did not pass validation is the defect being pinned.
    """
    from rtl_buddy.tools import pnr_openroad

    def _must_not_run(cmd, **_kwargs):
        cmd = list(cmd)
        if cmd[1:] == ["-v"]:
            result = MagicMock()
            result.returncode = 0
            result.stdout = "KLayout 0.30.8\n"
            result.stderr = ""
            return result
        raise AssertionError(f"a tool was launched: {cmd}")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _must_not_run)


def test_export_without_a_routed_def_fails_and_launches_nothing(tmp_path, monkeypatch):
    backend, artefacts = _export_backend(tmp_path, monkeypatch, routed=False)
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "export"
    assert "no routed DEF" in res.results["desc"]
    level, fields = _one_event(events, "pnr_export.no_def")
    assert fields["path"] == str(artefacts / "demo_top.def")


def test_export_with_an_empty_routed_def_fails(tmp_path, monkeypatch):
    """A zero-length DEF is a run that did not finish, not a design with
    nothing in it — and KLayout would stream it into a plausible layout."""
    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    (artefacts / "demo_top.def").write_text("")
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert "empty" in res.results["desc"]
    _one_event(events, "pnr_export.empty_def")


def test_export_refuses_a_def_that_belongs_to_another_design(tmp_path, monkeypatch):
    """The staleness check: the DEF's own `DESIGN` statement has to be the
    design this run's synth entry names (#618)."""
    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    (artefacts / "demo_top.def").write_text("DESIGN other_top ;\nEND DESIGN\n")
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert "other_top" in res.results["desc"]
    level, fields = _one_event(events, "pnr_export.def_stale")
    assert (fields["expected"], fields["found"]) == ("demo_top", "other_top")


def test_export_refuses_a_file_that_is_not_a_def(tmp_path, monkeypatch):
    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    (artefacts / "demo_top.def").write_text("this is not a DEF at all\n")
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert "DESIGN" in res.results["desc"]
    _one_event(events, "pnr_export.def_unreadable")


def test_export_reads_an_explicit_def_and_checks_its_design(tmp_path, monkeypatch):
    """`--def` exports a DEF from elsewhere, with the platform and the top
    still coming from the run's configuration."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, routed=False)
    elsewhere = tmp_path / "saved" / "demo_top.def"
    elsewhere.parent.mkdir()
    elsewhere.write_text("DESIGN demo_top ;\nEND DESIGN\n")
    seen: list[list[str]] = []
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", seen=seen),
    )

    res = backend.export_only(def_path=str(elsewhere))

    assert res.results["result"] == "PASS"
    stream = next(c for c in seen if any(a.startswith("in_def=") for a in c))
    assert f"in_def={elsewhere}" in stream
    assert res.results["gds_path"] == str(artefacts / "demo_top.gds")


def test_export_with_a_def_of_the_wrong_design_fails_before_klayout(
    tmp_path, monkeypatch
):
    backend, _artefacts = _export_backend(tmp_path, monkeypatch, routed=False)
    elsewhere = tmp_path / "saved" / "other.def"
    elsewhere.parent.mkdir()
    elsewhere.write_text("DESIGN other_top ;\n")
    _refuse_every_subprocess(monkeypatch)

    res = backend.export_only(def_path=str(elsewhere))

    assert res.results["result"] == "FAIL"
    assert "other_top" in res.results["desc"]


def test_export_without_klayout_fails_and_publishes_nothing(tmp_path, monkeypatch):
    """In export-only the render *is* the job, so a missing KLayout fails
    the command in preview mode too — unlike `rb pnr`, where it is a
    warning over a P&R verdict that stands."""
    backend, artefacts = _export_backend(tmp_path, monkeypatch, klayout=None)
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["gds_status"] == "failed"
    assert "KLayout" in res.results["desc"]
    assert not (artefacts / "demo_top.gds").exists()
    _one_event(events, "pnr.no_klayout")


def test_export_without_a_klayout_tech_fails(tmp_path, monkeypatch):
    backend, _artefacts = _export_backend(
        tmp_path,
        monkeypatch,
        pdk=_make_stream_pdk(tmp_path, klayout_tech=""),
    )
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert "klayout-tech" in res.results["desc"]
    _one_event(events, "pnr.gds_no_klayout_tech")


def test_export_with_a_configured_input_off_disk_fails(tmp_path, monkeypatch):
    """#617's gate, reached from the export-only path: every missing input
    is named, and KLayout is not launched."""
    pdk = _make_stream_pdk(tmp_path)
    Path(pdk.get_cell_gds()).unlink()
    backend, artefacts = _export_backend(tmp_path, monkeypatch, pdk=pdk)
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    level, fields = _one_event(events, "pnr.gds_missing_inputs")
    assert fields["missing"] == [pdk.get_cell_gds()]
    assert not (artefacts / "demo_top.gds").exists()


def test_export_with_a_missing_lyp_fails_before_anything_runs(tmp_path, monkeypatch):
    backend, artefacts = _export_backend(
        tmp_path,
        monkeypatch,
        emit_png=True,
        klayout_props=str(tmp_path / "nowhere" / "dark.lyp"),
    )
    (artefacts / "demo_top.gds").write_bytes(b"an older layout")
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert "dark.lyp" in res.results["desc"]
    _one_event(events, "pnr_export.no_lyp")
    # The sweep happens before the validation, so a failure on the inputs
    # leaves no earlier layout at the path a reader takes for this one's.
    assert not (artefacts / "demo_top.gds").exists()


def test_export_whose_synth_entry_cannot_be_resolved_fails_in_setup(
    tmp_path, monkeypatch
):
    """The design name comes from the synth *configuration*; when that
    cannot be read there is nothing to name the DEF or the GDS."""
    backend, _artefacts = _export_backend(tmp_path, monkeypatch)
    (tmp_path / "synth.yaml").unlink()
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    _one_event(events, "pnr_export.no_design")


# ---------------------------------------------------------------------------
# No stale successful outputs after a failure (#469)
# ---------------------------------------------------------------------------


def test_a_failed_export_removes_the_previous_export_s_outputs(tmp_path, monkeypatch):
    """A GDS and PNG from an earlier export must not survive a failure and
    be read as this one's result (#469)."""
    backend, artefacts = _export_backend(tmp_path, monkeypatch, emit_png=True)
    (artefacts / "demo_top.gds").write_bytes(b"older layout")
    (artefacts / "demo_top.png").write_bytes(b"older image")
    (artefacts / "def2stream.report.json").write_text('{"schema": 1}')
    (artefacts / "export.provenance.json").write_text('{"schema_version": 1}')
    (artefacts / "demo_top.def").unlink()
    _refuse_every_subprocess(monkeypatch)

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert not (artefacts / "demo_top.gds").exists()
    assert not (artefacts / "demo_top.png").exists()
    assert not (artefacts / "def2stream.report.json").exists()
    assert "gds_path" not in res.results
    # The record is rewritten rather than left describing the older export.
    record = json.loads((artefacts / "export.provenance.json").read_text())
    assert record["outcome"]["status"] == "failed"


def test_a_stream_out_that_writes_no_gds_fails_and_leaves_nothing(
    tmp_path, monkeypatch
):
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", gds=None, report=False, returncode=1),
    )

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["gds_status"] == "failed"
    assert not (artefacts / "demo_top.gds").exists()


def test_a_failed_render_fails_the_export_and_leaves_no_png(tmp_path, monkeypatch):
    """The GDS is complete, but `--png` asked for an image and there is
    none, so the export did not deliver."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, emit_png=True)
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top", png=None)
    )

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert "PNG render failed" in res.results["desc"]
    assert not (artefacts / "demo_top.png").exists()
    # Preview keeps the layout it did produce; the failure is the image.
    assert (artefacts / "demo_top.gds").exists()


# ---------------------------------------------------------------------------
# Completeness and the exit-code contract
# ---------------------------------------------------------------------------


def test_preview_publishes_an_incomplete_layout_as_a_qualified_pass(
    tmp_path, monkeypatch
):
    """Consistent with #619: an incomplete layout preview kept is reported
    as incomplete, not as a failure."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, mode="preview")
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", missing=["sram_32x64"]),
    )

    res = backend.export_only()

    assert res.results["result"] == "PASS"
    assert res.is_pass()
    assert res.results["gds_status"] == "incomplete"
    assert res.results["gds_missing_cells"] == ["sram_32x64"]
    assert "sram_32x64" in res.results["desc"]
    assert (artefacts / "demo_top.gds").exists()


def test_strict_refuses_to_publish_an_incomplete_layout(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, mode="strict")
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", missing=["sram_32x64"]),
    )

    res = backend.export_only()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "export"
    assert res.results["gds_missing_cells"] == ["sram_32x64"]
    assert not (artefacts / "demo_top.gds").exists()


def test_allowed_empty_cells_keep_an_export_complete(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    backend, _artefacts = _export_backend(tmp_path, monkeypatch, mode="strict")
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", allowed_empty=["fakeram45_64x32"]),
    )

    res = backend.export_only()

    assert res.results["result"] == "PASS"
    assert res.results["gds_status"] == "complete"
    assert res.results["gds_allowed_empty_cells"] == ["fakeram45_64x32"]


# ---------------------------------------------------------------------------
# PNG-only re-render
# ---------------------------------------------------------------------------


def _existing_layout(artefacts, design="demo_top", *, missing=()):
    """A published GDS with the stream-out report that vouches for it."""
    (artefacts / f"{design}.gds").write_bytes(_GDS_BYTES)
    (artefacts / "def2stream.report.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "design": design,
                "out_file": str(artefacts / f"{design}.gds"),
                "complete": not missing,
                "missing_cells": list(missing),
                "allowed_empty_cells": [],
                "orphan_cells": [],
                "other_errors": 0,
                "errors": len(missing),
            }
        )
    )


def test_png_only_rerenders_from_the_existing_gds_with_the_overrides(
    tmp_path, monkeypatch
):
    """The whole point of a re-render: no stream-out, and the layer
    properties and the resolution reach the KLayout command line (#618)."""
    from rtl_buddy.tools import pnr_openroad

    lyp = tmp_path / "dark.lyp"
    lyp.write_text("<layer-properties/>\n")
    backend, artefacts = _export_backend(
        tmp_path,
        monkeypatch,
        emit_png=True,
        klayout_props=str(lyp),
        png_width=800,
        png_height=600,
    )
    _existing_layout(artefacts)
    before = (artefacts / "demo_top.gds").read_bytes()
    seen: list[list[str]] = []
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", seen=seen, gds=None),
    )

    res = backend.export_only(png_only=True)

    assert res.results["result"] == "PASS"
    assert res.results["png_path"] == str(artefacts / "demo_top.png")
    render = next(c for c in seen if any(a.startswith("out_png=") for a in c))
    assert f"lyp_file={lyp}" in render
    assert "width=800" in render and "height=600" in render
    # No stream-out was launched, and the layout is untouched.
    assert not any(any(a.startswith("in_def=") for a in c) for c in seen)
    assert (artefacts / "demo_top.gds").read_bytes() == before


def test_png_only_carries_the_incomplete_qualifier_from_the_report(
    tmp_path, monkeypatch
):
    """A layout streamed with cells that had no GDS is incomplete however
    it is rendered, so the re-render says so too."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, mode="preview")
    _existing_layout(artefacts, missing=["sram_32x64"])
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", gds=None),
    )
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only(png_only=True)

    assert res.results["result"] == "PASS"
    assert res.results["gds_status"] == "incomplete"
    assert res.results["gds_missing_cells"] == ["sram_32x64"]
    level, fields = _one_event(events, "pnr_export.rerender_incomplete")
    assert fields["cells"] == ["sram_32x64"]


def test_strict_png_only_refuses_an_incomplete_layout_but_keeps_the_gds(
    tmp_path, monkeypatch
):
    """Strict publishes nothing it cannot vouch for — and withdraws only
    the image it made itself: the GDS was published by someone else."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, mode="strict")
    _existing_layout(artefacts, missing=["sram_32x64"])
    monkeypatch.setattr(
        pnr_openroad.subprocess,
        "run",
        _fake_klayout(backend, "demo_top", gds=None),
    )

    res = backend.export_only(png_only=True)

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "export"
    assert not (artefacts / "demo_top.png").exists()
    assert (artefacts / "demo_top.gds").read_bytes() == _GDS_BYTES
    assert (artefacts / "def2stream.report.json").exists()


def test_png_only_without_a_report_is_qualified_not_complete(tmp_path, monkeypatch):
    """Nothing vouched for that layout, so the re-render does not claim it
    is complete (#619) — preview renders it and says so."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch, mode="preview")
    (artefacts / "demo_top.gds").write_bytes(_GDS_BYTES)
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top", gds=None)
    )
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only(png_only=True)

    assert res.results["result"] == "PASS"
    assert res.results["gds_status"] == "incomplete"
    assert "completeness unknown" in res.results["desc"]
    _one_event(events, "pnr_export.rerender_unverified")


def test_png_only_without_a_gds_fails(tmp_path, monkeypatch):
    backend, _artefacts = _export_backend(tmp_path, monkeypatch)
    _refuse_every_subprocess(monkeypatch)
    events = _capture_pnr_events(monkeypatch)

    res = backend.export_only(png_only=True)

    assert res.results["result"] == "FAIL"
    assert "no GDS to re-render" in res.results["desc"]
    _one_event(events, "pnr_export.no_gds")


# ---------------------------------------------------------------------------
# The export record
# ---------------------------------------------------------------------------


def test_export_records_its_tool_inputs_and_outcome(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import EXPORT_PROVENANCE_SCHEMA

    backend, artefacts = _export_backend(tmp_path, monkeypatch, emit_png=True)
    monkeypatch.setattr(
        backend, "_probe_klayout_version", lambda _exe: "KLayout 0.30.8"
    )
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top")
    )
    def_bytes = (artefacts / "demo_top.def").read_bytes()

    res = backend.export_only()

    record = json.loads((artefacts / "export.provenance.json").read_text())
    assert record["schema_version"] == EXPORT_PROVENANCE_SCHEMA
    assert record["command"] == "pnr-export"
    assert record["run"] == "demo_pnr"
    assert record["top"] == "demo_top"
    assert record["gds_mode"] == "preview"
    assert record["png_only"] is False
    assert record["generator"].startswith("rtl-buddy ")
    assert record["generated_at"]
    assert record["tool"] == {
        "name": "klayout",
        "path": "/opt/klayout",
        "version": "KLayout 0.30.8",
    }
    assert record["inputs"]["def"]["sha256"] == hashlib.sha256(def_bytes).hexdigest()
    assert record["inputs"]["def"]["size"] == len(def_bytes)
    assert record["inputs"]["def"]["path"].endswith("demo_top.def")
    assert record["inputs"]["gds"] is None
    assert record["inputs"]["tech"].endswith("tech.lyt")
    assert [p.split("/")[-1] for p in record["inputs"]["cell_gds"]] == ["cells.gds"]
    assert [p.split("/")[-1] for p in record["inputs"]["lef"]] == [
        "tech.lef",
        "cells.lef",
    ]
    assert record["inputs"]["missing"] == []
    assert record["render"] == {
        "requested": True,
        "lyp": record["render"]["lyp"],
        "width": 2048,
        "height": 2048,
    }
    assert record["outputs"]["gds"].endswith("demo_top.gds")
    assert record["outputs"]["png"].endswith("demo_top.png")
    assert record["outcome"]["status"] == "complete"
    assert record["outcome"]["delivered"] is True
    # And the result points at it, so a machine consumer never has to guess
    # where the record went.
    assert res.results["export_provenance"] == str(artefacts / "export.provenance.json")


def test_a_png_only_record_names_the_gds_it_rendered(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    _existing_layout(artefacts)
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top", gds=None)
    )

    backend.export_only(png_only=True)

    record = json.loads((artefacts / "export.provenance.json").read_text())
    assert record["png_only"] is True
    assert record["inputs"]["def"] is None
    assert record["inputs"]["gds"]["sha256"] == hashlib.sha256(_GDS_BYTES).hexdigest()


def test_an_export_record_does_not_touch_the_pnr_run_s_own_records(
    tmp_path, monkeypatch
):
    """The export writes its own document and edits none of the run's."""
    from rtl_buddy.tools import pnr_openroad

    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    (artefacts / "pnr.log").write_text("OpenROAD said so\n")
    (artefacts / "pnr.tcl").write_text("# the flow this run ran\n")
    (artefacts / "timing.rpt").write_text("worst slack 0.5\n")
    (artefacts / "route.drc.rpt").write_text("")
    before = {
        name: (artefacts / name).read_text()
        for name in ("pnr.log", "pnr.tcl", "timing.rpt", "route.drc.rpt")
    }
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", _fake_klayout(backend, "demo_top")
    )

    backend.export_only()

    assert {name: (artefacts / name).read_text() for name in before} == before


def test_a_fresh_pnr_run_clears_a_previous_export_record(tmp_path, monkeypatch):
    """A run replaces an export rather than leaving its record vouching for
    a DEF the run is about to overwrite."""
    backend, artefacts = _export_backend(tmp_path, monkeypatch)
    (artefacts / "export.provenance.json").write_text('{"schema_version": 1}')

    backend._clear_stale_outputs(include_script=True)

    assert not (artefacts / "export.provenance.json").exists()


# ---------------------------------------------------------------------------
# Runner, tool-check and CLI wiring
# ---------------------------------------------------------------------------


def test_the_export_runner_never_resolves_the_pnr_tool(tmp_path, monkeypatch):
    """A box whose OpenROAD is absent or misconfigured can still export:
    the runner never asks `cfg-pnr-tools` for an executable (#618)."""
    from rtl_buddy.runner.pnr_runner import PnrExportRunner

    root_cfg = MagicMock()
    root_cfg.get_pnr_tool_cfg.side_effect = AssertionError(
        "the export resolved the P&R tool"
    )
    runner = PnrExportRunner(
        name="demo",
        root_cfg=root_cfg,
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        reglvl_filter=1000,
    )
    with monkeypatch.context() as m:
        m.setattr(
            "rtl_buddy.runner.pnr_runner.OpenRoadPnr",
            MagicMock(
                return_value=MagicMock(
                    export_only=MagicMock(return_value="stub-result")
                )
            ),
        )
        assert runner.run() == "stub-result"


def test_the_export_runner_skips_a_run_above_the_filter(tmp_path):
    from rtl_buddy.runner.pnr_runner import PnrExportRunner

    runner = PnrExportRunner(
        name="demo",
        root_cfg=MagicMock(),
        pnr_cfg=_make_pnr_cfg(tmp_path),
        suite_dir=str(tmp_path),
        reglvl_filter=100,
    )
    res = runner.run()
    assert res.results["result"] == "SKIP"


def test_tool_check_requires_klayout_for_the_export_but_not_openroad():
    """`rb tool-check --required-for pnr-export` gates on KLayout alone —
    the command runs no P&R and no synthesis (#618)."""
    from rtl_buddy.tool_manifest import get_manifest, subcommand_readiness

    subs = subcommand_readiness([], get_manifest())

    assert subs["pnr-export"]["tools"] == ["klayout"]
    assert "openroad" not in subs["pnr-export"]["tools"]
    assert "yosys" not in subs["pnr-export"]["tools"]
    # KLayout is an opt-in extra to `rb pnr`, which runs P&R without it,
    # and a hard requirement of the export that is nothing else.
    assert subs["pnr-export"]["optional_feature"] is False
    assert subs["pnr"]["optional_feature"] is True


def test_export_failures_have_dedicated_human_messages():
    """Every WARNING/ERROR event carries a sentence a user can act on,
    rather than the generic "event name plus fields" fallback."""
    from rtl_buddy.logging_utils import _human_message

    stale = _human_message(
        "pnr_export.def_stale",
        {
            "pnr": "demo_pnr",
            "path": "/a/demo_top.def",
            "expected": "demo_top",
            "found": "other_top",
        },
    )
    assert "other_top" in stale and "demo_top" in stale
    assert "rerun rb pnr" in stale

    no_def = _human_message(
        "pnr_export.no_def", {"pnr": "demo_pnr", "path": "/a/demo_top.def"}
    )
    assert "/a/demo_top.def" in no_def and "--def" in no_def

    incomplete = _human_message(
        "pnr_export.rerender_incomplete",
        {"pnr": "demo_pnr", "count": 2, "cells": ["sram_a", "sram_b"]},
    )
    assert "sram_a, sram_b" in incomplete


def test_the_machine_row_carries_the_export_status_and_record():
    from rtl_buddy.rtl_buddy import RtlBuddy
    from rtl_buddy.runner.pnr_results import PnrPassResults

    results = PnrPassResults(
        name="demo/export/results",
        desc="GDS incomplete: no layout for 1 cell (sram_32x64)",
        fields={
            "gds_path": "/a/demo_top.gds",
            "png_path": "/a/demo_top.png",
            "gds_mode": "preview",
            "gds_status": "incomplete",
            "gds_missing_cells": ["sram_32x64"],
            "gds_missing_cell_count": 1,
            "export_provenance": "/a/export.provenance.json",
        },
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})

    assert row["gds_status"] == "incomplete"
    assert row["gds_missing_cells"] == ["sram_32x64"]
    assert row["export_provenance"] == "/a/export.provenance.json"
    assert row["png_path"] == "/a/demo_top.png"


_ROOT_CONFIG_PNR = dedent("""\
    cfg-pdks:
      - name: "nangate45"
        site: "FreePDK45_38x28_10R_NP_162NW_34O"
        corners:
          typ: "pdk/lib/typ.lib"
        tech-lef: "pdk/lef/tech.lef"
        macro-lef: "pdk/lef/cells.lef"
        cell-gds: "pdk/gds/cells.gds"
        klayout-tech: "pdk/klayout/tech.lyt"
        klayout-props: "pdk/klayout/props.lyp"

    cfg-pnr-platforms:
      - name: "nangate45_typ"
        pdk: "nangate45"
        corner: "typ"
        cts-buffer: "BUF_X4"
        routing-layers:
          signal: "metal2-metal8"
          clock: "metal4-metal8"
""")


@pytest.fixture
def export_project(tmp_path, monkeypatch):
    """A project whose one P&R run has a saved routed result on disk."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "root_config.yaml").write_text(
        (_FIXTURES / "minimal_project" / "root_config.yaml").read_text()
        + "\n"
        + _ROOT_CONFIG_PNR
    )
    (root / "regression.yaml").write_text(
        (_FIXTURES / "minimal_project" / "regression.yaml").read_text()
    )
    _make_stream_pdk(root)
    _write_synth_config(root)
    (root / "pnr.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: "demo_pnr"
            desc: "demo"
            tool: "openroad"
            synth: "demo_synth"
            synth-path: "synth.yaml"
            constraints: "constraints.sdc"
            platform: "nangate45_typ"
            reglvl: 0
          - name: "demo_pnr_alt"
            desc: "a second entry, so a selection can span more than one run"
            tool: "openroad"
            synth: "demo_synth"
            synth-path: "synth.yaml"
            constraints: "constraints.sdc"
            platform: "nangate45_typ"
            reglvl: 0
        """)
    )
    artefacts = root / "artefacts" / "demo_pnr"
    artefacts.mkdir(parents=True)
    for name, text in _ROUTED.items():
        (artefacts / name.format(design="demo_top")).write_text(
            text.format(design="demo_top")
        )
    monkeypatch.chdir(root)
    return root, artefacts


_LIVE = []


def _runner():
    """A fresh CLI object, with the previous one's artefact lock released."""
    from typer.testing import CliRunner
    from rtl_buddy.rtl_buddy import RtlBuddy

    while _LIVE:
        _LIVE.pop()._artifact_locks.release_all()
    rb = RtlBuddy(name="test_pnr_export")
    _LIVE.append(rb)
    return CliRunner(), rb


def _flat(output: str) -> str:
    """The console output with Rich's wrapping folded away (#570)."""
    return " ".join(output.split())


def _klayout_only(monkeypatch, artefacts, **kwargs):
    """Fake KLayout, and hand every other command to the real runner.

    `subprocess.run` is process-global — `pnr_openroad.subprocess` *is*
    the module — and a CLI invocation shells out to `uname` while
    RootConfig is loading and to `git` for the machine envelope. Only the
    commands that start with the (patched) KLayout path are answered here.
    """
    from rtl_buddy.tools import pnr_openroad

    real = pnr_openroad.subprocess.run
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: "/opt/klayout")
    fake = _fake_klayout(MagicMock(artefact_dir=str(artefacts)), "demo_top", **kwargs)

    def _run(cmd, **kw):
        if os.path.basename(str(list(cmd)[0])) == "klayout":
            return fake(cmd, **kw)
        return real(cmd, **kw)

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)


def test_cli_exports_a_saved_result_and_reports_the_outputs(
    export_project, monkeypatch
):
    root, artefacts = export_project
    _klayout_only(monkeypatch, artefacts)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["pnr-export", "demo_pnr", "-c", "pnr.yaml"])

    assert result.exit_code == 0, result.output
    assert "P&R Export Results Summary" in _flat(result.output)
    assert "gds" in _flat(result.output)
    assert (artefacts / "demo_top.gds").exists()


def test_cli_machine_output_carries_the_export_row(export_project, monkeypatch):
    root, artefacts = export_project
    _klayout_only(monkeypatch, artefacts, missing=["sram_32x64"])
    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["--machine", "pnr-export", "demo_pnr", "-c", "pnr.yaml"]
    )

    payload = json.loads(
        next(
            line for line in result.output.splitlines() if line.startswith('{"command"')
        )
    )
    assert payload["command"] == "pnr-export"
    assert payload["exit_code"] == 0
    row = payload["payload"]["results"][0]
    assert row["name"] == "demo_pnr"
    assert row["result"] == "PASS"
    assert row["gds_status"] == "incomplete"
    assert row["gds_missing_cells"] == ["sram_32x64"]
    assert row["export_provenance"].endswith("export.provenance.json")


def test_cli_fails_with_a_non_zero_exit_when_the_export_is_not_delivered(
    export_project, monkeypatch
):
    """In export-only the export is the job: no KLayout, no export, exit 1
    — even in the default preview mode."""
    from rtl_buddy.tools import pnr_openroad

    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: None)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["pnr-export", "demo_pnr", "-c", "pnr.yaml"])

    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_cli_def_needs_a_single_named_run(export_project):
    """One DEF cannot be several runs' saved result, and guessing which is
    exactly the staleness this command refuses."""
    from rtl_buddy.errors import FatalRtlBuddyError

    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["pnr-export", "-c", "pnr.yaml", "--def", "saved/x.def"]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "--def needs exactly one pnr run" in str(result.exception)


def test_cli_lists_the_runs_without_touching_the_artefacts(export_project):
    root, artefacts = export_project
    before = _routed_digests(artefacts)
    runner, rb = _runner()

    result = runner.invoke(rb.app, ["pnr-export", "-c", "pnr.yaml", "--list"])

    assert result.exit_code == 0
    assert "demo_pnr" in result.output
    assert _routed_digests(artefacts) == before


# ---------------------------------------------------------------------------
# --checkpoint (#653)
# ---------------------------------------------------------------------------


def test_cli_checkpoint_needs_a_single_named_run(export_project):
    from rtl_buddy.errors import FatalRtlBuddyError

    runner, rb = _runner()

    result = runner.invoke(
        rb.app, ["pnr-export", "-c", "pnr.yaml", "--checkpoint", "cts"]
    )

    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "--checkpoint needs exactly one pnr run" in str(result.exception)


def test_cli_checkpoint_and_def_are_exclusive(export_project):
    from rtl_buddy.errors import FatalRtlBuddyError

    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["pnr-export", "demo_pnr", "-c", "pnr.yaml", "--checkpoint", "cts"]
        + ["--def", "saved/x.def"],
    )

    assert isinstance(result.exception, FatalRtlBuddyError), result.output
    assert "exclusive" in str(result.exception)


def test_cli_machine_row_labels_a_checkpoint_export(export_project, monkeypatch):
    """The row names the checkpoint and says it is not final; the routed
    result beside it is untouched."""
    root, artefacts = export_project
    before = _routed_digests(artefacts)
    run_dir = artefacts / "checkpoints" / "20260925T101500-42"
    run_dir.mkdir(parents=True)
    (run_dir / "02_place.def").write_text("DESIGN demo_top ;\nEND DESIGN\n")
    (run_dir / "progress.jsonl").write_text(
        json.dumps(
            {
                "event": "checkpoint",
                "stage": "place",
                "index": "02",
                "status": "ok",
                "design": "demo_top",
                "files": {"def": "02_place.def"},
            }
        )
        + "\n"
    )
    os.symlink(run_dir.name, artefacts / "checkpoints" / "latest")
    export_dir = run_dir / "export" / "02_place"
    _klayout_only(monkeypatch, export_dir)
    export_dir.mkdir(parents=True)
    runner, rb = _runner()

    result = runner.invoke(
        rb.app,
        ["--machine", "pnr-export", "demo_pnr", "-c", "pnr.yaml"]
        + ["--checkpoint", "place"],
    )

    payload = json.loads(
        next(
            line for line in result.output.splitlines() if line.startswith('{"command"')
        )
    )
    row = payload["payload"]["results"][0]
    assert row["result"] == "PASS", row
    assert row["checkpoint_stage"] == "02_place"
    assert row["checkpoint_run_id"] == run_dir.name
    assert row["checkpoint_final"] is False
    assert "not final" in row["desc"]
    assert row["gds_path"] == str(export_dir / "demo_top.gds")
    assert _routed_digests(artefacts) == before
