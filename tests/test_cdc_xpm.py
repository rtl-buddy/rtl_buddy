"""XPM CDC macro recognition end to end through `rb cdc`.

XPM CDC macros ship in the vendor install tree, so the analyzer sees a bodyless dual-clock blackbox; rtl-buddy-cdc 0.4.0 and later recognises the family by module name. Hermetic tests run the checked-in `cdc_xpm_macro_*.json` fixtures through the pure emit and audit functions. Live tests run the installed `rtl-buddy-cdc` and skip unless `lint --help` advertises `--sync-primitive`.

Regenerate the fixtures from the repository root, so `location.file` stays repo-relative:

    rtl-buddy-cdc lint --top cdc_xpm_macro_top
      --sdc tests/fixtures/cdc/cdc_xpm_macro_top.sdc
      --format json --output tests/fixtures/cdc/cdc_xpm_macro_report.json
      --emit-domain-map tests/fixtures/cdc/cdc_xpm_macro_domain_map.json
      tests/fixtures/cdc/cdc_xpm_macro_top.sv
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from rtl_buddy.tools.cdc_constraints import generate_constraints
from rtl_buddy.tools.cdc_xdc_audit import audit_xdc, extract_cdc_constraints

FIX = Path(__file__).parent / "fixtures" / "cdc"
TOP = "cdc_xpm_macro_top"

# Capability marker for the live tests: the flag that shipped with XPM recognition.
XPM_CAPABILITY_FLAG = "--sync-primitive"

# Documented floor for the feature, used in the skip message.
XPM_MIN_CDC_VERSION = "0.4.0"


@functools.lru_cache(maxsize=1)
def _cdc_supports_xpm() -> bool:
    """True if the installed rtl-buddy-cdc recognises the XPM CDC family.

    Probes `lint --help` for `--sync-primitive`; any failure to run the probe means unsupported.
    """
    exe = shutil.which("rtl-buddy-cdc")
    if exe is None:
        return False
    try:
        probe = subprocess.run(
            [exe, "lint", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # Both streams and no return-code check, matching `cdc_rtl_buddy._lint_supports_project_root`. Extra conditions would fail closed and skip silently forever.
    return XPM_CAPABILITY_FLAG in (probe.stdout + probe.stderr)


requires_xpm_engine = pytest.mark.skipif(
    not _cdc_supports_xpm(),
    reason=(
        f"installed rtl-buddy-cdc does not recognise the xpm_cdc_* family "
        f"(no {XPM_CAPABILITY_FLAG} in `lint --help`); needs "
        f">= {XPM_MIN_CDC_VERSION}"
    ),
)

requires_yosys = pytest.mark.skipif(
    shutil.which("yosys") is None,
    reason="yosys is not on PATH (rtl-buddy-cdc lint needs an elaboration frontend)",
)


@pytest.fixture
def domain_map() -> dict:
    return json.loads((FIX / "cdc_xpm_macro_domain_map.json").read_text())


@pytest.fixture
def cdc_report() -> dict:
    return json.loads((FIX / "cdc_xpm_macro_report.json").read_text())


def test_xpm_report_is_clean(cdc_report) -> None:
    """Every crossing is carried by an XPM macro, so the report is empty, with no `CDC-BBX`."""
    assert cdc_report["summary"]["violations"] == 0
    assert [v["rule_id"] for v in cdc_report["violations"]] == []


def test_xpm_domain_map_has_no_async_crossings(domain_map) -> None:
    """The macro absorbs the crossing, so the map has no async crossings; the two clock domains stay described."""
    assert domain_map["crossings"] == []
    assert {c["name"] for c in domain_map["clocks"]} == {"clk_a", "clk_b"}
    assert domain_map["clock_groups"]


def test_emit_constraints_frames_clocks_without_per_crossing_exceptions(
    domain_map,
) -> None:
    """`--emit-constraints` emits clock framing and the async group, and no per-crossing exceptions."""
    r = generate_constraints(domain_map, {}, fmt="xdc", scoped=False)
    kinds = Counter(e["kind"] for e in r.manifest)
    assert kinds["create_clock"] == 2
    assert kinds["clock_groups"] == 1
    assert kinds["max_delay"] == 0
    assert kinds["bus_skew"] == 0

    assert "create_clock -name clk_a -period 8.0" in r.text
    assert "create_clock -name clk_b -period 10.0" in r.text
    assert "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}" in r.text


def test_emitted_xdc_audits_clean(domain_map, cdc_report) -> None:
    """The emitted XDC audits clean under `--check-xdc` with no `recognized-syncs` list."""
    emitted = generate_constraints(domain_map, {}, fmt="xdc", scoped=False)
    xc = extract_cdc_constraints(emitted.text)
    result = audit_xdc(domain_map, cdc_report, xc)
    assert result.findings == []
    assert result.blockers == []


def test_audit_needs_no_recognize_sync_for_xpm(domain_map, cdc_report) -> None:
    """An XDC declaring the two clocks async is complete without `--recognize-sync`."""
    xdc = (
        "create_clock -name clk_a -period 8.0 [get_ports {clk_a}]\n"
        "create_clock -name clk_b -period 10.0 [get_ports {clk_b}]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    result = audit_xdc(domain_map, cdc_report, extract_cdc_constraints(xdc))
    assert result.blockers == []


def test_checked_in_maps_came_from_a_recognising_engine(domain_map) -> None:
    """The checked-in maps came from an engine that recognises XPM; an older release would give the same empty crossing list because the run failed on CDC-BBX."""
    assert domain_map["generator"]["name"] == "rtl-buddy-cdc"
    # Compare as integer tuples: `"0.10.0" >= "0.4.0"` is False as strings, and `packaging` is not a dependency.
    got = tuple(int(part) for part in domain_map["generator"]["version"].split(".")[:3])
    want = tuple(int(part) for part in XPM_MIN_CDC_VERSION.split("."))
    assert got >= want, f"fixture map came from rtl-buddy-cdc {got}, needs >= {want}"


@requires_yosys
@requires_xpm_engine
def test_installed_engine_lints_xpm_design_clean(tmp_path: Path) -> None:
    """An XPM design lints clean with the installed engine; skipped until the engine recognises XPM."""
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [
            "rtl-buddy-cdc",
            "lint",
            "--top",
            TOP,
            "--sdc",
            str(FIX / f"{TOP}.sdc"),
            "--format",
            "json",
            "--output",
            str(report),
            str(FIX / f"{TOP}.sv"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = json.loads(report.read_text())
    assert live["summary"]["violations"] == 0
    assert [v["rule_id"] for v in live["violations"]] == []


@requires_yosys
@requires_xpm_engine
def test_installed_engine_reproduces_the_checked_in_map(
    tmp_path: Path, domain_map
) -> None:
    """The installed engine reproduces the checked-in map's `crossings`, `clocks`, `clock_groups` and `design.top`; the generator version is not compared."""
    emitted = tmp_path / "domain-map.json"
    proc = subprocess.run(
        [
            "rtl-buddy-cdc",
            "lint",
            "--top",
            TOP,
            "--sdc",
            str(FIX / f"{TOP}.sdc"),
            "--format",
            "json",
            "--output",
            str(tmp_path / "report.json"),
            "--emit-domain-map",
            str(emitted),
            str(FIX / f"{TOP}.sv"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = json.loads(emitted.read_text())
    assert live["crossings"] == domain_map["crossings"]
    assert live["clocks"] == domain_map["clocks"]
    assert live["clock_groups"] == domain_map["clock_groups"]
    assert live["design"]["top"] == TOP


def test_capability_probe_reports_a_bool() -> None:
    """The probe never raises; a missing or broken tool means unsupported."""
    assert isinstance(_cdc_supports_xpm(), bool)
