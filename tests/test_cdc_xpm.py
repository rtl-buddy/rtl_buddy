"""XPM CDC macro recognition end to end through `rb cdc`.

XPM CDC macros ship in the vendor install tree, so the analyzer sees a bodyless dual-clock blackbox; rtl-buddy-cdc 0.4.0 and later recognises the family by module name. Hermetic tests run the checked-in `cdc_xpm_macro_*.json` fixtures through the pure emit and audit functions. Live tests run the installed `rtl-buddy-cdc` and skip unless `lint --help` advertises `--sync-primitive`.

Regenerate the fixtures from the repository root, so `location.file` stays repo-relative:

    rtl-buddy-cdc lint --top cdc_xpm_macro_top \\
        --sdc tests/fixtures/cdc/cdc_xpm_macro_top.sdc \\
        --format json --output tests/fixtures/cdc/cdc_xpm_macro_report.json \\
        --emit-domain-map tests/fixtures/cdc/cdc_xpm_macro_domain_map.json \\
        tests/fixtures/cdc/cdc_xpm_macro_top.sv

The separate `cdc_xpm_top` fixture models `xpm_cdc_single` as a bare flop to test the `--check-xdc --recognize-sync` escape hatch, which stays valid for macros outside the XPM family.
"""

from __future__ import annotations

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


@pytest.fixture(scope="module")
def xpm_engine() -> str:
    """Probe capabilities only when running the live engine contract."""
    if shutil.which("yosys") is None:
        pytest.skip(
            "yosys is not on PATH (rtl-buddy-cdc lint needs an elaboration frontend)"
        )
    exe = shutil.which("rtl-buddy-cdc")
    if exe is None:
        pytest.skip("rtl-buddy-cdc is not on PATH")
    try:
        probe = subprocess.run(
            [exe, "lint", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"rtl-buddy-cdc capability probe failed: {exc}")
    # Both streams and no return-code check, matching `cdc_rtl_buddy._lint_supports_project_root`. Extra conditions would fail closed and skip silently forever.
    if XPM_CAPABILITY_FLAG not in (probe.stdout + probe.stderr):
        pytest.skip(
            f"installed rtl-buddy-cdc does not recognise the xpm_cdc_* family "
            f"(no {XPM_CAPABILITY_FLAG} in `lint --help`); needs "
            f">= {XPM_MIN_CDC_VERSION}"
        )
    return exe


@pytest.fixture
def domain_map() -> dict:
    return json.loads((FIX / "cdc_xpm_macro_domain_map.json").read_text())


@pytest.fixture
def cdc_report() -> dict:
    return json.loads((FIX / "cdc_xpm_macro_report.json").read_text())


def test_xpm_fixtures_describe_recognized_macros(domain_map, cdc_report) -> None:
    """Recognized macros absorb the crossings while preserving both clock domains."""
    assert cdc_report["summary"]["violations"] == 0
    assert [v["rule_id"] for v in cdc_report["violations"]] == []
    assert domain_map["crossings"] == []
    assert {c["name"] for c in domain_map["clocks"]} == {"clk_a", "clk_b"}
    assert domain_map["clock_groups"]
    assert domain_map["generator"]["name"] == "rtl-buddy-cdc"
    got = tuple(int(part) for part in domain_map["generator"]["version"].split(".")[:3])
    want = tuple(int(part) for part in XPM_MIN_CDC_VERSION.split("."))
    assert got >= want, f"fixture map came from rtl-buddy-cdc {got}, needs >= {want}"


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


def test_installed_engine_lints_xpm_and_reproduces_the_checked_in_map(
    tmp_path: Path, domain_map, xpm_engine: str
) -> None:
    """The installed engine reproduces the checked-in map's `crossings`, `clocks`, `clock_groups` and `design.top`; the generator version is not compared."""
    emitted = tmp_path / "domain-map.json"
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [
            xpm_engine,
            "lint",
            "--top",
            TOP,
            "--sdc",
            str(FIX / f"{TOP}.sdc"),
            "--format",
            "json",
            "--output",
            str(report),
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
    live_report = json.loads(report.read_text())
    assert live_report["summary"]["violations"] == 0
    assert [v["rule_id"] for v in live_report["violations"]] == []
    live = json.loads(emitted.read_text())
    assert live["crossings"] == domain_map["crossings"]
    assert live["clocks"] == domain_map["clocks"]
    assert live["clock_groups"] == domain_map["clock_groups"]
    assert live["design"]["top"] == TOP
