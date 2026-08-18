"""XPM CDC macro recognition end-to-end through `rb cdc` (#315).

A Vivado design synchronises with the Xilinx XPM CDC macros
(`xpm_cdc_single`, `xpm_cdc_gray`, ...), whose sources ship inside the
vendor install tree — so a filelist built from project RTL carries only
the instantiation and the analyzer sees a bodyless, dual-clock blackbox.

Until rtl-buddy-cdc learned to recognise the family by module name
(rtl-buddy-cdc#275, released in **0.4.0**), each macro was declined as
"not provably single-clock" and reported as a `CDC-BBX` error, so
`--check-xdc` / `--emit-constraints` on an XPM design were unusable —
the macro *is* the synchroniser, but the engine did not know it. That is
the gap this module's fixtures pin.

Two tiers, deliberately:

- **Hermetic** tests drive the checked-in `cdc_xpm_macro_domain_map.json`
  / `cdc_xpm_macro_report.json` (produced by rtl-buddy-cdc 0.4.0) through
  the pure emit / audit functions. They run everywhere, matching the "no
  live tool: the maps are the contract" pattern of `test_cdc_constraints`
  and `test_cdc_check_xdc`.
- **Live** tests invoke the installed `rtl-buddy-cdc` and are gated on a
  *capability probe*, not a version number: they skip unless
  `lint --help` advertises `--sync-primitive`, the flag that shipped
  alongside XPM recognition. Version floors go stale and lie about
  pre-release builds; the help surface is the thing that actually
  predicts whether the run will work. The same probe-the-help-surface
  idiom is already used by `cdc_rtl_buddy._lint_supports_project_root`,
  and this module matches it exactly — both streams, no return-code
  condition — so the two probes cannot drift into failing closed.

Regenerate the fixtures from the repository ROOT, so the emitted
`location.file` entries stay repo-relative and a regeneration is a no-op
diff rather than four lines of someone's home directory:

    rtl-buddy-cdc lint --top cdc_xpm_macro_top \
        --sdc tests/fixtures/cdc/cdc_xpm_macro_top.sdc \
        --format json --output tests/fixtures/cdc/cdc_xpm_macro_report.json \
        --emit-domain-map tests/fixtures/cdc/cdc_xpm_macro_domain_map.json \
        tests/fixtures/cdc/cdc_xpm_macro_top.sv

Nothing here reads `flop_domains[*].location`, and
`test_installed_engine_reproduces_the_checked_in_map` deliberately compares
only `crossings` / `clocks` / `clock_groups` / `design.top` — so the paths
are documentation, not contract. They are still checked-in bytes, though,
and a machine-specific absolute path is noise every regeneration has to
undo by hand.

Note the older `cdc_xpm_top` fixture in the same directory is NOT
superseded: it models `xpm_cdc_single` as a bare single flop to exercise
the `--check-xdc --recognize-sync` escape hatch, which is how a user told
the *audit* about a macro the *engine* could not recognise. That hatch
remains valid for any macro outside the XPM family.
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

# The flag that shipped with XPM recognition (rtl-buddy-cdc#275, 0.4.0).
# Used as the capability marker for the live tests below.
XPM_CAPABILITY_FLAG = "--sync-primitive"

# Documented floor for the feature, for the skip message and the docs.
XPM_MIN_CDC_VERSION = "0.4.0"


@functools.lru_cache(maxsize=1)
def _cdc_supports_xpm() -> bool:
    """True iff the installed rtl-buddy-cdc recognises the XPM CDC family.

    Probes `lint --help` for the `--sync-primitive` flag rather than
    parsing a version string: a capability probe stays honest across
    pre-release builds, forks and editable installs, where a version
    comparison would either lie or need a special case. Any failure to
    run the probe degrades to "unsupported" — the same defensive shape
    `cdc_rtl_buddy._lint_supports_project_root` uses.
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
    # Match the cited idiom exactly: both streams, and no return-code
    # condition. The point of a capability probe is that the engine is NOT
    # pinned by rtl_buddy, so "this version prints help on stdout and exits
    # 0" is precisely the assumption that cannot be made. Both extra
    # conditions fail CLOSED — the gate would skip forever and nobody would
    # notice, the one failure mode a capability probe must not have.
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


# --------------------------------------------------------------------------
# Hermetic: the checked-in maps are the contract
# --------------------------------------------------------------------------


def test_xpm_report_is_clean(cdc_report) -> None:
    """The headline. Every crossing in the design is carried by an XPM
    macro, so a recognising engine reports nothing at all — in
    particular no `CDC-BBX`, which is what an XPM design used to drown
    in (one per macro instance)."""
    assert cdc_report["summary"]["violations"] == 0
    assert [v["rule_id"] for v in cdc_report["violations"]] == []


def test_xpm_domain_map_has_no_async_crossings(domain_map) -> None:
    """The macro absorbs the crossing, so it is not left dangling in the
    map for the audit to flag as unconstrained. The clock framing is
    still fully described — the design really does have two async
    domains, and the XDC still has to say so."""
    assert domain_map["crossings"] == []
    assert {c["name"] for c in domain_map["clocks"]} == {"clk_a", "clk_b"}
    assert domain_map["clock_groups"]


def test_emit_constraints_frames_clocks_without_per_crossing_exceptions(
    domain_map,
) -> None:
    """`--emit-constraints` on an XPM design emits the clock framing and
    the async group, and *no* per-crossing `set_max_delay` / bus-skew
    exceptions — there is no unsynchronised crossing left to except.
    Before XPM recognition this path could not run at all: the run
    failed on CDC-BBX before a map was worth reading."""
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
    """Round-trip: feed `--emit-constraints` output straight back into
    `--check-xdc`. No gaps, no over-waives — and notably no
    `recognized-syncs` list is needed, which is exactly what #315 set
    out to retire for the XPM family."""
    emitted = generate_constraints(domain_map, {}, fmt="xdc", scoped=False)
    xc = extract_cdc_constraints(emitted.text)
    result = audit_xdc(domain_map, cdc_report, xc)
    assert result.findings == []
    assert result.blockers == []


def test_audit_needs_no_recognize_sync_for_xpm(domain_map, cdc_report) -> None:
    """An XDC that declares the two clocks async is complete for this
    design. The older `cdc_xpm_top` fixture needed `--recognize-sync` to
    reach the same verdict; a recognising engine makes the escape hatch
    unnecessary for the XPM family."""
    xdc = (
        "create_clock -name clk_a -period 8.0 [get_ports {clk_a}]\n"
        "create_clock -name clk_b -period 10.0 [get_ports {clk_b}]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    result = audit_xdc(domain_map, cdc_report, extract_cdc_constraints(xdc))
    assert result.blockers == []


def test_checked_in_maps_came_from_a_recognising_engine(domain_map) -> None:
    """Guards the fixture itself. These maps only mean what the tests
    above claim if they were produced by an engine that recognises XPM —
    a map regenerated with an older release would show the same empty
    crossing list for the wrong reason (the run failed on CDC-BBX)."""
    assert domain_map["generator"]["name"] == "rtl-buddy-cdc"
    # Tuple compare, not string: `"0.10.0" >= "0.4.0"` is False in Python,
    # so a lexicographic guard starts failing on the first two-digit minor
    # for a reason that has nothing to do with XPM recognition — while
    # claiming the fixture came from a non-recognising engine. `packaging`
    # is not a dependency of this repo, and these are plain dotted numbers.
    got = tuple(int(part) for part in domain_map["generator"]["version"].split(".")[:3])
    want = tuple(int(part) for part in XPM_MIN_CDC_VERSION.split("."))
    assert got >= want, f"fixture map came from rtl-buddy-cdc {got}, needs >= {want}"


# --------------------------------------------------------------------------
# Live: does the installed engine actually do this?
# --------------------------------------------------------------------------


@requires_yosys
@requires_xpm_engine
def test_installed_engine_lints_xpm_design_clean(tmp_path: Path) -> None:
    """End-to-end against the real tool: an XPM design lints clean.

    Skipped until an engine with XPM recognition is installed, so CI
    stays green on the current release and this lights up on upgrade.
    """
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
    """The checked-in map is a real artefact, not a hand-written one.

    Compares the meaningful content rather than the file bytes — the
    generator version legitimately moves with every release.
    """
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
    """The probe must never raise — a missing / broken tool degrades to
    'unsupported' so the gate skips rather than erroring the session."""
    assert isinstance(_cdc_supports_xpm(), bool)
