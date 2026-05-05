"""
Unit tests for _parse_verilator_metric output format handling.

Verilator ≤5.042 emits:  "Total coverage (hit/total) X.XX%"
Verilator ≥5.048 emits:  "  toggle    : 63.1% ( 82/130)"
"""

import pytest
from unittest.mock import MagicMock, patch

from rtl_buddy.tools.vlog_cov import VlogCov


def _make_cov():
    return VlogCov(simulator_name="verilator", use_lcov=True)


def _run_parse(output_text, metric_name="toggle"):
    """Drive _parse_verilator_metric with a fake subprocess result."""
    cov = _make_cov()
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = output_text
    fake_result.stderr = ""

    # _build_annotate_cwd needs a real-ish dat file; stub it out
    with (
        patch.object(cov, "_build_annotate_cwd", return_value="/tmp/fake_cwd"),
        patch("subprocess.run", return_value=fake_result),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = MagicMock(return_value="/tmp/fake_tmpdir")
        mock_td.return_value.__exit__ = MagicMock(return_value=False)
        return cov._parse_verilator_metric("/fake/coverage.dat", metric_name)


def test_legacy_format_total_coverage_line():
    output = (
        "Coverage Summary:\n"
        "  toggle    : 63.1% ( 82/130)\n"
        "Total coverage (82/130) 63.10%\n"
    )
    result = _run_parse(output)
    assert result == pytest.approx(82 / 130)


def test_new_format_per_metric_table():
    output = (
        "Coverage Summary:\n"
        "  line      : 0.0% (  0/  0)\n"
        "  toggle    : 63.1% ( 82/130)\n"
        "  branch    : 0.0% (  0/  0)\n"
        "  expr      : 0.0% (  0/  0)\n"
    )
    result = _run_parse(output)
    assert result == pytest.approx(82 / 130)


def test_new_format_fully_covered():
    output = "Coverage Summary:\n  toggle    : 100.0% (130/130)\n"
    result = _run_parse(output)
    assert result == pytest.approx(1.0)


def test_new_format_zero_hit():
    output = "Coverage Summary:\n  toggle    : 0.0% (  0/130)\n"
    result = _run_parse(output)
    assert result == pytest.approx(0.0)


def test_new_format_zero_total_returns_none():
    output = "Coverage Summary:\n  toggle    : 0.0% (  0/  0)\n"
    result = _run_parse(output)
    assert result is None


def test_missing_metric_returns_none_with_warning(caplog):
    import logging

    output = "Coverage Summary:\n  line      : 75.0% ( 15/ 20)\n"
    with caplog.at_level(logging.WARNING):
        result = _run_parse(output, metric_name="toggle")
    assert result is None
    assert any(
        "summary_missing" in r.message or "toggle" in r.message for r in caplog.records
    )
