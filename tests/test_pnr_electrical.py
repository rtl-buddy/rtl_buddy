"""Tests for port buffering (`buffer-ports:`) and the routed design's electrical violators (rtl_buddy#772).

A hardened block buffers its ports by default, so its Liberty pins do not carry the fanout behind them. Max-slew, max-capacitance and max-fanout violator counts are result fields; `fail-on-electrical:` turns them into a verdict.
"""

import shutil
import subprocess
from textwrap import dedent

import pytest

from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

from test_multi_corner import _pnr_backend_over
from test_pnr import _make_pdk_cfg, _make_pnr_cfg, _platform, _render_flow

_PORT_BUFFERS = 'puts ">>> Port buffers"\nbuffer_ports -inputs -outputs\n'


def _run(tmp_path, extra=""):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent("""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: blk
            desc: block
            synth: s
            synth-path: synth.yaml
            platform: p
        """)
        + extra
    )
    return PnrSuiteConfig(str(path)).get_runs("blk")[0]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("", False),
        ("    harden: true\n", True),
        ("    harden: true\n    buffer-ports: false\n", False),
        ("    buffer-ports: true\n", True),
        ("    harden: false\n    buffer-ports: false\n", False),
    ],
)
def test_buffer_ports_follows_harden_unless_set(tmp_path, extra, expected):
    assert _run(tmp_path, extra).get_buffer_ports() is expected


def test_fail_on_electrical_defaults_off_and_parses(tmp_path):
    assert _run(tmp_path).get_fail_on_electrical() is False
    assert (
        _run(tmp_path, "    fail-on-electrical: true\n").get_fail_on_electrical()
        is True
    )


def test_a_run_without_port_buffers_renders_the_pin_to_placement_step_unchanged(
    tmp_path,
):
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))
    assert "buffer_ports" not in text
    assert (
        "place_pins -hor_layers $PIN_LAYER_H -ver_layers $PIN_LAYER_V\n\n"
        'puts ">>> Global placement"'
    ) in text


@pytest.mark.parametrize(
    "overrides", [{"harden": True}, {"buffer_ports": True}], ids=["harden", "set"]
)
def test_port_buffers_go_between_pin_placement_and_global_placement(
    tmp_path, overrides
):
    """As ORFS global_place.tcl: after `place_pins`, before `global_placement`, so the
    placer places the buffers and `repair_design` sizes them.
    """
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)), **overrides)
    buffers = text.index(_PORT_BUFFERS)
    assert text.index("place_pins -hor_layers") < buffers
    assert (
        buffers < text.index("global_placement -density") < text.index("repair_design")
    )
    assert text.count("buffer_ports") == 1


def test_a_hardened_run_can_turn_port_buffers_off(tmp_path):
    text = _render_flow(
        tmp_path, _platform(_make_pdk_cfg(tmp_path)), harden=True, buffer_ports=False
    )
    assert "buffer_ports" not in text


def test_the_final_reports_count_electrical_violators(tmp_path):
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))
    counts = text.index(
        'puts "RB-ELECTRICAL: max_$rb_check [sta::max_${rb_check}_violation_count]"'
    )
    assert text.index(">>> Final reports") < counts < text.index(">>> Write outputs")
    assert "foreach rb_check {slew capacitance fanout}" in text
    assert (
        "report_check_types -max_slew -max_capacitance -max_fanout -violators "
        "> $OUT_DIR/electrical.rpt"
    ) in text


def test_a_failing_violator_count_does_not_stop_the_flow(tmp_path):
    """The counts are report-only; an OpenROAD without the `sta::` counter loses them, not the routed database."""
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        pytest.skip("no tclsh")
    text = _render_flow(tmp_path, _platform(_make_pdk_cfg(tmp_path)))
    start = text.index("foreach rb_check")
    end = text.index('puts ">>> Write outputs"')
    script = (
        "set OUT_DIR /nonexistent\n" + text[start:end] + 'puts "REACHED_WRITE_DB"\n'
    )
    out = subprocess.run([tclsh], input=script, capture_output=True, text=True)
    assert "REACHED_WRITE_DB" in out.stdout
    assert "rb: max capacitance violator count unavailable" in out.stdout
    assert "rb: electrical.rpt unavailable" in out.stdout


def test_violator_counts_are_parsed_from_the_log(tmp_path):
    backend = OpenRoadPnr("demo/openroad", _make_pnr_cfg(tmp_path), str(tmp_path), None)
    log = (
        "RB-ELECTRICAL: max_slew 12\n"
        "RB-ELECTRICAL: max_capacitance 3\n"
        "rb: max fanout violator count unavailable: invalid command name\n"
    )
    fields = backend._parse_electrical(log)
    assert fields == {
        "max_slew_violation_count": 12,
        "max_capacitance_violation_count": 3,
    }
    assert (
        backend._describe_electrical(fields)
        == "12 max-slew, 3 max-capacitance violator(s)"
    )
    assert backend._describe_electrical({"max_slew_violation_count": 0}) == ""


_CLEAN = (
    "worst slack max 1.0\n"
    "RB-ELECTRICAL: max_slew 0\n"
    "RB-ELECTRICAL: max_capacitance 0\n"
    "RB-ELECTRICAL: max_fanout 0\n"
)
_DIRTY = (
    "worst slack max -3.07\n"
    "RB-ELECTRICAL: max_slew 2\n"
    "RB-ELECTRICAL: max_capacitance 1\n"
    "RB-ELECTRICAL: max_fanout 0\n"
)


def test_a_clean_run_records_zero_violators_and_reads_as_a_plain_pass(
    tmp_path, monkeypatch
):
    platform = _platform(_make_pdk_cfg(tmp_path))
    res = _pnr_backend_over(tmp_path, monkeypatch, platform, _CLEAN).run()

    assert isinstance(res, PnrPassResults)
    assert res.results["max_slew_violation_count"] == 0
    assert res.results["max_capacitance_violation_count"] == 0
    assert res.results["max_fanout_violation_count"] == 0
    assert "electrical" not in res.results["desc"]


def test_violators_qualify_a_pass_and_warn(tmp_path, monkeypatch, caplog):
    """Off by default, violators still pass, but never as a clean pass."""
    platform = _platform(_make_pdk_cfg(tmp_path))
    with caplog.at_level("WARNING"):
        res = _pnr_backend_over(tmp_path, monkeypatch, platform, _DIRTY).run()

    assert isinstance(res, PnrPassResults)
    assert res.results["max_slew_violation_count"] == 2
    assert res.results["max_capacitance_violation_count"] == 1
    assert "electrical 2 max-slew, 1 max-capacitance violator(s)" in res.results["desc"]
    assert any(
        r.levelname == "WARNING"
        and "2 max-slew, 1 max-capacitance and 0 max-fanout violator(s); "
        "the run still passes"
        in r.getMessage()
        for r in caplog.records
    )


def test_fail_on_electrical_fails_a_run_with_violators(tmp_path, monkeypatch):
    platform = _platform(_make_pdk_cfg(tmp_path))
    backend = _pnr_backend_over(tmp_path, monkeypatch, platform, _DIRTY)
    backend.pnr_cfg.fail_on_electrical = True

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["desc"] == (
        "electrical violators in the routed design: 2 max-slew, 1 max-capacitance "
        "violator(s) (fail-on-electrical; see electrical.rpt)"
    )
    # A verdict on the design, which an `xfail:` may excuse.
    assert "fail_stage" not in res.results
    assert res.results["max_slew_violation_count"] == 2
    assert res.results["wns_setup_ps"] == pytest.approx(-3070.0)


def test_fail_on_electrical_passes_a_clean_run(tmp_path, monkeypatch):
    platform = _platform(_make_pdk_cfg(tmp_path))
    backend = _pnr_backend_over(tmp_path, monkeypatch, platform, _CLEAN)
    backend.pnr_cfg.fail_on_electrical = True

    assert backend.run().is_pass()


def test_pnr_row_carries_the_violator_counts():
    from rtl_buddy.rtl_buddy import RtlBuddy

    results = PnrPassResults(
        name="demo/results",
        fields={
            "max_slew_violation_count": 2,
            "max_capacitance_violation_count": 1,
            "max_fanout_violation_count": 0,
        },
    )
    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": results})
    assert row["max_slew_violation_count"] == 2
    assert row["max_capacitance_violation_count"] == 1
    assert row["max_fanout_violation_count"] == 0


def test_buffered_ports_join_the_hardened_config_digest(tmp_path):
    """Port buffers change the block's pins, so abstracts hardened without them go
    stale; an explicit `buffer-ports: false` keeps the old digest.
    """
    platform = _platform(_make_pdk_cfg(tmp_path))
    old = pnr_abstract.abstract_config(
        _make_pnr_cfg(tmp_path, harden=True, buffer_ports=False), platform
    )
    assert "buffer_ports" not in old

    default = pnr_abstract.abstract_config(
        _make_pnr_cfg(tmp_path, harden=True), platform
    )
    assert default["buffer_ports"] is True
    assert pnr_abstract.config_digest(default) != pnr_abstract.config_digest(old)

    # A run-time verdict, not a physical change.
    strict = pnr_abstract.abstract_config(
        _make_pnr_cfg(tmp_path, harden=True, fail_on_electrical=True), platform
    )
    assert pnr_abstract.config_digest(strict) == pnr_abstract.config_digest(default)


def test_the_platform_port_buffer_names_the_buffer_cell(tmp_path):
    platform = _platform(_make_pdk_cfg(tmp_path), port_buffer="BUF_X2")
    assert platform.get_port_buffer() == "BUF_X2"
    text = _render_flow(tmp_path, platform, harden=True)
    assert "buffer_ports -inputs -outputs -buffer_cell BUF_X2\n" in text
    # A run that does not buffer its ports ignores it.
    assert "buffer_ports" not in _render_flow(tmp_path, platform)


@pytest.mark.parametrize("bad", ["", "BUF_X1 BUF_X2", ["BUF_X1"]])
def test_a_malformed_port_buffer_is_refused(tmp_path, bad):
    from rtl_buddy.errors import FatalRtlBuddyError

    with pytest.raises(FatalRtlBuddyError, match="port-buffer must be one buffer"):
        _platform(_make_pdk_cfg(tmp_path), port_buffer=bad)


def test_the_port_buffer_joins_the_digest_only_when_ports_are_buffered(tmp_path):
    pdk = _make_pdk_cfg(tmp_path)
    plain = _platform(pdk)
    picked = _platform(pdk, port_buffer="BUF_X2")

    buffered = _make_pnr_cfg(tmp_path, harden=True)
    assert "port_buffer" not in pnr_abstract.abstract_config(buffered, plain)
    assert pnr_abstract.abstract_config(buffered, picked)["port_buffer"] == "BUF_X2"

    unbuffered = _make_pnr_cfg(tmp_path, harden=True, buffer_ports=False)
    assert pnr_abstract.config_digest(
        pnr_abstract.abstract_config(unbuffered, picked)
    ) == pnr_abstract.config_digest(pnr_abstract.abstract_config(unbuffered, plain))
