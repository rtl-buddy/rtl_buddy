"""`signoff:`: the corners each timing side is judged at, the gates that fail a routed run, and the post-route rounds judged on those corners (rtl_buddy#900)."""

import json
import re
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
import yaml
from test_multi_corner import _pdk as _mc_pdk
from test_multi_corner import _platform as _mc_platform
from test_pnr import _make_pdk_cfg, _make_pnr_cfg, _platform
from test_pnr_checkpoints import _runs
from test_pnr_repair import _done, _repair_backend, _rounds

from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.config.pnr_repair import PnrMode
from rtl_buddy.config.pnr_signoff import Signoff
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.logging_utils import _human_message
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.runner.xfail import apply_xfail
from rtl_buddy.tools import pnr_abstract, pnr_checkpoints, pnr_openroad
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

# -- configuration ---------------------------------------------------------------


def _run(tmp_path, signoff=..., **keys):
    """Load one `pnr.yaml` run, with `signoff` as its block when given (`None` writes `signoff: null`)."""
    run = {
        "name": "r",
        "desc": "d",
        "synth": "s",
        "synth-path": "synth.yaml",
        "platform": "pl",
        **keys,
    }
    if signoff is not ...:
        run["signoff"] = signoff
    path = tmp_path / "pnr.yaml"
    path.write_text(yaml.safe_dump({"rtl-buddy-filetype": "pnr_config", "runs": [run]}))
    return PnrSuiteConfig(str(path)).get_runs("r")[0]


def _mc(tmp_path, **kw):
    return _mc_platform(_mc_pdk(tmp_path), sta_corners=["tt", "ss", "ff"], **kw)


def _resolved(tmp_path, platform, signoff=...):
    run = _run(tmp_path, signoff)
    return platform.with_run_overrides(run.get_overrides()).get_signoff()


def test_no_block_means_no_signoff(tmp_path):
    platform = _mc(tmp_path)
    assert platform.get_signoff() is None
    assert _resolved(tmp_path, platform) is None


def test_corners_default_to_every_corner_and_fail_on_to_no_gate(tmp_path):
    assert _resolved(tmp_path, _mc(tmp_path), {}) == Signoff(
        setup_corners=("tt", "ss", "ff"), hold_corners=("tt", "ss", "ff"), fail_on=()
    )


def test_a_run_block_names_corners_and_gates_in_the_platform_s_order(tmp_path):
    signoff = _resolved(
        tmp_path,
        _mc(tmp_path),
        {
            "setup-corners": ["ss"],
            "hold-corners": ["ff", "tt"],
            "fail-on": ["electrical", "timing"],
        },
    )
    assert signoff == Signoff(
        setup_corners=("ss",),
        hold_corners=("tt", "ff"),
        fail_on=("timing", "electrical"),
    )


def test_a_run_overrides_the_platform_field_by_field(tmp_path):
    platform = _mc(
        tmp_path,
        signoff={"setup-corners": ["ss"], "hold-corners": ["ff"], "fail-on": ["drc"]},
    )
    assert platform.get_signoff() == Signoff(("ss",), ("ff",), ("drc",))
    assert _resolved(tmp_path, platform, {"fail-on": ["timing"]}) == Signoff(
        ("ss",), ("ff",), ("timing",)
    )
    # `null` for a key is its default; `null` for the block turns it off.
    assert _resolved(tmp_path, platform, {"setup-corners": None}) == Signoff(
        ("tt", "ss", "ff"), ("ff",), ("drc",)
    )
    run = _run(tmp_path, None)
    assert platform.with_run_overrides(run.get_overrides()).get_signoff() is None
    # A run without the block keeps the platform's.
    assert _resolved(tmp_path, platform) == platform.get_signoff()


def test_a_single_corner_platform_s_corner_is_the_default_and_may_be_named(tmp_path):
    platform = _platform(_make_pdk_cfg(tmp_path), sta_corner="typ")
    assert _resolved(tmp_path, platform, {"fail-on": ["timing"]}) == Signoff(
        ("typ",), ("typ",), ("timing",)
    )
    assert _resolved(tmp_path, platform, {"setup-corners": ["typ"]}) == Signoff(
        ("typ",), ("typ",), ()
    )


def test_a_run_naming_an_unknown_corner_is_refused_naming_it(tmp_path):
    with pytest.raises(FatalRtlBuddyError) as exc:
        _resolved(tmp_path, _mc(tmp_path), {"hold-corners": ["ff", "sf"]})
    assert str(exc.value) == (
        "pnr run 'r': signoff.hold-corners names corner(s) sf, which platform "
        "'sky130hd_mc' does not analyse; its corners: tt, ss, ff"
    )


def test_a_run_naming_an_unknown_corner_fails_setup(tmp_path, monkeypatch):
    res = _signoff_run(tmp_path, monkeypatch, {"setup-corners": ["sf"]}).run()

    assert res.results["result"] == "FAIL"
    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"].startswith(
        "pnr run 'r': signoff.setup-corners names corner(s) sf"
    )


def test_a_platform_naming_an_unknown_corner_is_refused_at_load(tmp_path):
    with pytest.raises(
        FatalRtlBuddyError,
        match=r"pnr platform 'sky130hd_mc': signoff.setup-corners names corner\(s\) "
        r"slow, which platform 'sky130hd_mc' does not analyse",
    ):
        _mc(tmp_path, signoff={"setup-corners": ["slow"]})


@pytest.mark.parametrize(
    ("block", "words"),
    [
        (
            {"setup-corners": "ss"},
            "signoff.setup-corners is a list, e.g. setup-corners: [ss]",
        ),
        ({"hold-corners": []}, "signoff.hold-corners must be a non-empty list"),
        ({"hold-corners": ["ff", "ff"]}, "signoff.hold-corners lists ff twice"),
        (
            {"fail-on": ["timing", "lvs"]},
            "names unknown gate(s) lvs; the gates are timing, drc, electrical",
        ),
        ({"fail-on": "drc"}, "signoff.fail-on is a list, e.g. fail-on: [drc]"),
        ("timing", "signoff must be a mapping of setup-corners, hold-corners, fail-on"),
    ],
)
def test_a_bad_value_is_refused_at_load_naming_the_key(tmp_path, block, words):
    with pytest.raises(FatalRtlBuddyError) as exc:
        _run(tmp_path, block)
    assert str(exc.value).startswith("pnr run 'r': signoff")
    assert words in str(exc.value)


def test_fail_on_may_be_empty(tmp_path):
    assert _resolved(tmp_path, _mc(tmp_path), {"fail-on": []}).fail_on == ()


# -- the result and its gates -------------------------------------------------------


_MC_LOG = """\
>>> Final reports
Design area 2580 um^2 43% utilization.
worst slack max -0.12
worst slack min -0.05
tns max -1.00
tns min -0.05
>>> Per-corner timing
corner tt worst slack max 0.30
corner tt worst slack min 0.10
corner ss worst slack max -0.12
corner ss worst slack min 0.40
corner ff worst slack max 0.50
corner ff worst slack min -0.05
RB-ELECTRICAL: max_slew 0
RB-ELECTRICAL: max_capacitance 0
RB-ELECTRICAL: max_fanout 0
>>> Write outputs
"""


def _backend(tmp_path, monkeypatch, platform, log, *, drcs=0, **cfg):
    """A backend whose OpenROAD writes `log` and a detailed-route DRC report of `drcs` violations (none when `drcs` is None), and whose routed DEF and database exist."""
    (tmp_path / "models.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: model_config
        models:
          - name: "demo_top"
            filelist: []
        """)
    )
    (tmp_path / "synth.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: synth_config
        syntheses:
          - name: "demo_synth"
            desc: "demo"
            model: "demo_top"
            model_path: "models.yaml"
            tool: "openroad"
            reglvl: 0
        """)
    )
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform
    backend = OpenRoadPnr(
        name="demo/openroad",
        pnr_cfg=_make_pnr_cfg(tmp_path, **cfg),
        suite_dir=str(tmp_path),
        root_cfg=root_cfg,
    )
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(
        backend, "_write_script", lambda *a, **kw: backend._script_path()
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    monkeypatch.setattr(backend, "_probe_sta_modes", lambda: True)

    def _fake_run(cmd, **kwargs):
        kwargs["stdout"].write(log + ">>> DONE\n")
        artefacts = Path(backend.artefact_dir)
        if drcs is not None:
            (artefacts / "route.drc.rpt").write_text(
                "".join(f"violation type: Short\n\tbbox {i}\n" for i in range(drcs))
            )
        for name in ("demo_top.def", "demo_top.routed.odb"):
            (artefacts / name).write_text("routed\n")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)
    return backend


def _signoff_run(tmp_path, monkeypatch, signoff, log=_MC_LOG, *, platform=None, **kw):
    platform = platform or _mc(tmp_path)
    overrides = _run(tmp_path, signoff).get_overrides()
    return _backend(tmp_path, monkeypatch, platform, log, overrides=overrides, **kw)


def test_the_record_carries_each_gate_s_values_and_verdict(tmp_path, monkeypatch):
    res = _signoff_run(
        tmp_path,
        monkeypatch,
        {"setup-corners": ["tt", "ff"], "hold-corners": ["ss", "tt"]},
        drcs=2,
    ).run()

    # Nothing gated: the run passes, with every value reported.
    assert isinstance(res, PnrPassResults)
    assert res.results["signoff"] == {
        "timing": {
            "setup": {
                "corners": ["tt", "ff"],
                "wns_ps": pytest.approx(300.0),
                "worst_corner": "tt",
                "unconstrained": False,
            },
            "hold": {
                "corners": ["tt", "ss"],
                "wns_ps": pytest.approx(100.0),
                "worst_corner": "tt",
                "unconstrained": False,
            },
            "verdict": "not_gated",
        },
        "drc": {"count": 2, "verdict": "not_gated"},
        "electrical": {
            "max_slew_violation_count": 0,
            "max_capacitance_violation_count": 0,
            "max_fanout_violation_count": 0,
            "verdict": "not_gated",
        },
    }
    # The scalars stay the worst over every corner.
    assert res.results["wns_setup_ps"] == pytest.approx(-120.0)
    assert res.results["worst_setup_corner"] == "ss"


@pytest.mark.parametrize(
    ("signoff", "verdict", "desc"),
    [
        ({"setup-corners": ["tt", "ff"], "hold-corners": ["tt", "ss"]}, "pass", None),
        (
            {"setup-corners": ["tt", "ss"], "hold-corners": ["ss"]},
            "fail",
            "signoff failed: timing (setup WNS -0.1200 ns at ss)",
        ),
        (
            {"setup-corners": ["ff"], "hold-corners": ["ff"]},
            "fail",
            "signoff failed: timing (hold WNS -0.0500 ns at ff)",
        ),
        (
            {},
            "fail",
            "signoff failed: timing (setup WNS -0.1200 ns at ss, hold WNS "
            "-0.0500 ns at ff)",
        ),
    ],
    ids=["met", "setup", "hold", "every-corner"],
)
def test_the_timing_gate_judges_each_side_at_its_corners(
    tmp_path, monkeypatch, signoff, verdict, desc
):
    res = _signoff_run(tmp_path, monkeypatch, {**signoff, "fail-on": ["timing"]}).run()

    assert res.results["signoff"]["timing"]["verdict"] == verdict
    if desc is None:
        assert res.results["result"] == "PASS"
    else:
        assert isinstance(res, PnrFailResults)
        assert res.results["desc"] == desc
        # A verdict on the design, which an `xfail:` may excuse.
        assert "fail_stage" not in res.results


def test_the_timing_gate_on_a_single_corner_platform(tmp_path, monkeypatch):
    platform = _platform(_make_pdk_cfg(tmp_path), sta_corner="typ")
    log = "worst slack max -0.25\nworst slack min 0.10\n"
    res = _signoff_run(
        tmp_path, monkeypatch, {"fail-on": ["timing"]}, log, platform=platform
    ).run()

    assert res.results["desc"] == "signoff failed: timing (setup WNS -0.2500 ns at typ)"
    assert res.results["signoff"]["timing"]["setup"] == {
        "corners": ["typ"],
        "wns_ps": pytest.approx(-250.0),
        "worst_corner": "typ",
        "unconstrained": False,
    }
    assert res.results["signoff"]["timing"]["hold"]["wns_ps"] == pytest.approx(100.0)


def _typ(tmp_path):
    return _platform(_make_pdk_cfg(tmp_path), sta_corner="typ")


def test_an_unconstrained_hold_passes_and_an_unreported_one_fails(
    tmp_path, monkeypatch
):
    log = "worst slack max 0.20\nworst slack min INF\n"
    res = _signoff_run(
        tmp_path, monkeypatch, {"fail-on": ["timing"]}, log, platform=_typ(tmp_path)
    ).run()

    timing = res.results["signoff"]["timing"]
    assert timing["hold"]["unconstrained"] is True
    assert timing["hold"]["wns_ps"] is None
    assert res.results["result"] == "PASS"

    res = _signoff_run(
        tmp_path / "b",
        monkeypatch,
        {"fail-on": ["timing"]},
        "worst slack max 0.20\n",
        platform=_typ(tmp_path),
    ).run()
    assert res.results["desc"] == "signoff failed: timing (hold WNS not reported)"


def test_an_unconstrained_setup_fails_the_timing_gate(tmp_path, monkeypatch):
    log = "worst slack max INF\nworst slack min 0.10\n"
    res = _signoff_run(
        tmp_path, monkeypatch, {"fail-on": ["timing"]}, log, platform=_typ(tmp_path)
    ).run()

    timing = res.results["signoff"]["timing"]
    assert timing["setup"]["unconstrained"] is True
    assert timing["verdict"] == "fail"
    assert res.results["desc"] == "signoff failed: timing (setup unconstrained at typ)"


def test_setup_inf_at_every_judged_corner_fails_and_at_one_passes(
    tmp_path, monkeypatch
):
    inf_ss = _MC_LOG.replace(
        "corner ss worst slack max -0.12", "corner ss worst slack max INF"
    ).replace("worst slack max -0.12", "worst slack max 0.30")
    inf_ss_tt = inf_ss.replace(
        "corner tt worst slack max 0.30", "corner tt worst slack max INF"
    )
    block = {"setup-corners": ["tt", "ss"], "hold-corners": ["tt"], "fail-on": ["timing"]}

    res = _signoff_run(tmp_path / "a", monkeypatch, block, inf_ss).run()
    assert res.results["result"] == "PASS"
    assert res.results["signoff"]["timing"]["setup"]["worst_corner"] == "tt"

    res = _signoff_run(tmp_path / "b", monkeypatch, block, inf_ss_tt).run()
    assert res.results["desc"] == (
        "signoff failed: timing (setup unconstrained at tt, ss)"
    )


def test_a_side_inf_at_its_corners_is_unconstrained(tmp_path, monkeypatch):
    log = _MC_LOG.replace(
        "corner ff worst slack min -0.05", "corner ff worst slack min INF"
    )
    res = _signoff_run(
        tmp_path,
        monkeypatch,
        {"hold-corners": ["ff"], "setup-corners": ["tt"], "fail-on": ["timing"]},
        log,
    ).run()

    assert res.results["result"] == "PASS"
    assert res.results["signoff"]["timing"]["hold"]["unconstrained"] is True


@pytest.mark.parametrize(
    ("setup", "hold", "verdict", "desc"),
    [
        ("0.00", "0.00", "pass", None),
        ("0.01", "0.02", "pass", None),
        (
            "-0.00",
            "0.00",
            "fail",
            "signoff failed: timing (setup WNS -0.0000 ns at typ)",
        ),
        (
            "0.00",
            "-0.00",
            "fail",
            "signoff failed: timing (hold WNS -0.0000 ns at typ)",
        ),
    ],
    ids=["zero", "positive", "negative-zero-setup", "negative-zero-hold"],
)
def test_a_wns_of_negative_zero_fails_the_timing_gate(
    tmp_path, monkeypatch, setup, hold, verdict, desc
):
    """The final reports print two digits, so a slack in (-5 ps, 0) reads `-0.00`."""
    log = f"worst slack max {setup}\nworst slack min {hold}\n"
    res = _signoff_run(
        tmp_path, monkeypatch, {"fail-on": ["timing"]}, log, platform=_typ(tmp_path)
    ).run()

    assert res.results["signoff"]["timing"]["verdict"] == verdict
    if desc is None:
        assert res.results["result"] == "PASS"
    else:
        assert res.results["desc"] == desc


def test_negative_zero_at_a_later_corner_is_the_worst(tmp_path, monkeypatch):
    log = _MC_LOG.replace(
        "corner tt worst slack max 0.30", "corner tt worst slack max 0.00"
    ).replace("corner ss worst slack max -0.12", "corner ss worst slack max -0.00")
    res = _signoff_run(
        tmp_path,
        monkeypatch,
        {"setup-corners": ["tt", "ss"], "hold-corners": ["tt"], "fail-on": ["timing"]},
        log,
    ).run()

    assert res.results["signoff"]["timing"]["setup"]["worst_corner"] == "ss"
    assert res.results["desc"] == "signoff failed: timing (setup WNS -0.0000 ns at ss)"


@pytest.mark.parametrize(("drcs", "verdict"), [(0, "pass"), (3, "fail")])
def test_the_drc_gate_fails_on_the_detailed_route_s_violations(
    tmp_path, monkeypatch, drcs, verdict
):
    res = _signoff_run(tmp_path, monkeypatch, {"fail-on": ["drc"]}, drcs=drcs).run()

    assert res.results["drc_count"] == drcs
    assert res.results["signoff"]["drc"] == {"count": drcs, "verdict": verdict}
    assert res.results["signoff"]["timing"]["verdict"] == "not_gated"
    if drcs:
        assert res.results["desc"] == "signoff failed: drc (3 violation(s))"
    else:
        assert res.results["result"] == "PASS"


_DIRTY = _MC_LOG.replace("RB-ELECTRICAL: max_slew 0", "RB-ELECTRICAL: max_slew 2")


@pytest.mark.parametrize(("log", "verdict"), [(_MC_LOG, "pass"), (_DIRTY, "fail")])
def test_the_electrical_gate_fails_on_violators(tmp_path, monkeypatch, log, verdict):
    res = _signoff_run(tmp_path, monkeypatch, {"fail-on": ["electrical"]}, log).run()

    assert res.results["signoff"]["electrical"]["verdict"] == verdict
    if verdict == "fail":
        assert res.results["desc"] == (
            "signoff failed: electrical (2 max-slew violator(s); see electrical.rpt)"
        )
    else:
        assert res.results["result"] == "PASS"


def test_the_description_names_each_failed_gate_and_the_run_keeps_its_outputs(
    tmp_path, monkeypatch, caplog
):
    backend = _signoff_run(
        tmp_path,
        monkeypatch,
        {"setup-corners": ["ss"], "fail-on": ["timing", "drc", "electrical"]},
        _DIRTY,
        drcs=1,
    )
    with caplog.at_level("WARNING"):
        res = backend.run()

    desc = (
        "signoff failed: timing (setup WNS -0.1200 ns at ss, hold WNS -0.0500 ns at "
        "ff); drc (1 violation(s)); electrical (2 max-slew violator(s); see "
        "electrical.rpt)"
    )
    assert res.results["desc"] == desc
    assert "fail_stage" not in res.results
    artefacts = Path(backend.artefact_dir)
    assert (artefacts / "demo_top.def").is_file()
    assert (artefacts / "demo_top.routed.odb").is_file()
    [record] = [
        r for r in caplog.records if getattr(r, "rtl_event", "") == "pnr.signoff_failed"
    ]
    assert _human_message("pnr.signoff_failed", record.rtl_fields) == (
        f"pnr demo_pnr: {desc}"
    )


def test_the_result_row_carries_the_record(tmp_path, monkeypatch):
    from rtl_buddy.rtl_buddy import RtlBuddy

    res = _signoff_run(tmp_path, monkeypatch, {"fail-on": ["drc"]}).run()

    row = RtlBuddy._pnr_result_row(None, {"pnr_name": "demo", "results": res})
    assert row["signoff"] == res.results["signoff"]


def test_xfail_excuses_a_failed_gate(tmp_path, monkeypatch):
    res = _signoff_run(tmp_path, monkeypatch, {"fail-on": ["timing"]}).run()

    apply_xfail(res, match="signoff failed: timing")

    assert res.results["result"] == "XFAIL"
    assert res.is_pass()


# -- fail-on-electrical -------------------------------------------------------------


def test_fail_on_electrical_without_the_block_keeps_its_result(tmp_path, monkeypatch):
    res = _backend(
        tmp_path, monkeypatch, _mc(tmp_path), _DIRTY, fail_on_electrical=True
    ).run()

    assert res.results["desc"] == (
        "electrical violators in the routed design: 2 max-slew violator(s) "
        "(fail-on-electrical; see electrical.rpt)"
    )
    assert "signoff" not in res.results


@pytest.mark.parametrize(
    ("signoff", "fail_on_electrical"),
    [({"fail-on": ["electrical"]}, False), ({}, True), ({"fail-on": ["drc"]}, True)],
    ids=["fail-on", "flag", "union"],
)
def test_fail_on_electrical_is_the_electrical_gate(
    tmp_path, monkeypatch, signoff, fail_on_electrical
):
    backend = _signoff_run(
        tmp_path, monkeypatch, signoff, _DIRTY, fail_on_electrical=fail_on_electrical
    )
    res = backend.run()

    assert res.results["signoff"]["electrical"]["verdict"] == "fail"
    assert res.results["desc"].startswith("signoff failed: electrical (2 max-slew")
    assert backend._gates_electrical() is True
    if "drc" in signoff.get("fail-on", []):
        assert backend._gates() == ("drc", "electrical")


# -- stopped and probe runs ----------------------------------------------------------


_PR = {"setup-margin": 0.0, "hold-margin": 0.0, "rounds": 2}


def _with_run_signoff(backend, signoff):
    """Give a stub-harness backend's run the `signoff:` block `signoff`."""
    overrides = _run(Path(backend.suite_dir), signoff).get_overrides()
    backend.pnr_cfg = replace(backend.pnr_cfg, overrides=overrides)


def test_a_stopped_run_reports_the_values_it_has_and_gates_nothing(
    tmp_path, monkeypatch
):
    backend, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"setup-margin": 0.0, "rounds": 1},
        corners=["ss", "ff"],
        env={
            "RB_STUB_SETUP": "-0.2 -0.3",
            "RB_STUB_SETUP_ss": "-0.2",
            "RB_STUB_SETUP_ff": "0.3",
        },
        stop_after="post_route_repair",
    )
    _with_run_signoff(backend, {"fail-on": ["timing", "drc", "electrical"]})

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["stopped_after"] == "post_route_repair"
    signoff = res.results["signoff"]
    assert signoff["timing"]["setup"]["wns_ps"] == pytest.approx(-200.0)
    assert signoff["timing"]["setup"]["worst_corner"] == "ss"
    assert signoff["drc"] == {"count": 0, "verdict": "not_gated"}
    assert signoff["electrical"]["max_slew_violation_count"] is None
    assert {gate["verdict"] for gate in signoff.values()} == {"not_gated"}


def test_a_probe_reports_without_gating(tmp_path, monkeypatch):
    backend, _artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route=_PR,
        mode=PnrMode.PROBE,
        corners=["ss", "ff"],
        env={"RB_STUB_SETUP_ss": "-0.4", "RB_STUB_HOLD_ff": "-0.1"},
    )
    _with_run_signoff(backend, {"fail-on": ["timing", "drc"]})

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    assert res.results["mode"] == "probe"
    signoff = res.results["signoff"]
    assert signoff["drc"] == {"count": None, "verdict": "not_gated"}
    assert signoff["timing"]["verdict"] == "not_gated"
    assert signoff["timing"]["setup"]["wns_ps"] == pytest.approx(-400.0)
    assert signoff["timing"]["hold"]["wns_ps"] == pytest.approx(-100.0)


# -- post-route rounds -------------------------------------------------------------


# Hold is worst at ss, a corner the block does not sign hold off at.
_HOLD_AT_SS = {
    "RB_STUB_SETUP": "0.1 0.1",
    "RB_STUB_HOLD": "-0.3 -0.3",
    "RB_STUB_SETUP_ss": "0.1 0.1",
    "RB_STUB_SETUP_ff": "0.4 0.4",
    "RB_STUB_HOLD_ss": "-0.3 -0.3",
    "RB_STUB_HOLD_ff": "0.05 0.05",
}


def test_without_signoff_hold_at_any_corner_drives_hold_repair(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, corners=["ss", "ff"], env=_HOLD_AT_SS
    )

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    assert _done(run_dir)["repaired_rounds"] == 1
    assert "judged_corners" not in _done(run_dir)
    assert all("signoff_timing" not in r for r in _rounds(run_dir))
    assert "signoff" not in res.results
    plan = json.loads((run_dir / "06_post_route_repair.plan.json").read_text())
    assert "judged" not in plan
    assert "the rounds judge" not in Path(backend._script_path()).read_text()


def test_rounds_judge_hold_only_at_the_hold_corners(tmp_path, monkeypatch):
    backend, artefacts = _repair_backend(
        tmp_path, monkeypatch, post_route=_PR, corners=["ss", "ff"], env=_HOLD_AT_SS
    )
    _with_run_signoff(backend, {"setup-corners": ["ss"], "hold-corners": ["ff"]})

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    done = _done(run_dir)
    assert done["stop"] == "timing_met"
    assert done["repaired_rounds"] == 0
    assert done["judged_corners"] == {"setup": ["ss"], "hold": ["ff"]}
    [round_0] = _rounds(run_dir)
    # The event keeps the worst over every corner and adds the judged one.
    assert round_0["wns_hold_ps"] == pytest.approx(-300.0)
    assert round_0["signoff_timing"] == {
        "wns_setup_ps": pytest.approx(100.0),
        "wns_hold_ps": pytest.approx(50.0),
    }
    extract = (run_dir / "06_post_route_repair.r0.extract.tcl").read_text()
    assert 'puts ">>> Post-route repair: the rounds judge setup at ss, hold at ff"' in (
        extract
    )


def test_the_kept_round_is_judged_on_the_setup_corners_only(tmp_path, monkeypatch):
    """Round 1 improves setup at ss and worsens it at ff, which the block does not sign setup off at, so it is kept."""
    backend, artefacts = _repair_backend(
        tmp_path,
        monkeypatch,
        post_route={"setup-margin": 0.0, "rounds": 1},
        corners=["ss", "ff"],
        env={
            "RB_STUB_SETUP": "-0.3 -0.5",
            "RB_STUB_SETUP_ss": "-0.3 -0.1",
            "RB_STUB_SETUP_ff": "-0.2 -0.5",
        },
    )
    _with_run_signoff(backend, {"setup-corners": ["ss"]})

    res = backend.run()

    assert res.results["result"] == "PASS", res.results
    (run_dir,) = _runs(artefacts)
    done = _done(run_dir)
    assert done["kept_round"] == 1
    assert done["stop"] == "last_round"
    assert [r["best_round"] for r in _rounds(run_dir)] == [0, 1]


def test_judged_timing_takes_each_side_over_its_corners():
    timing = {
        "wns_setup_ps": -300.0,
        "wns_hold_ps": -50.0,
        "unconstrained": [],
        "corners": {
            "ss": {"wns_setup_ps": -300.0, "wns_hold_ps": 20.0},
            "ff": {"wns_setup_ps": 100.0},
        },
        "max_slew_violation_count": 1,
    }
    text = "corner ff worst slack min INF\n"
    view = pnr_openroad._judged_timing(timing, text, {"setup": ["ff"], "hold": ["ff"]})
    assert view["wns_setup_ps"] == 100.0
    assert view["wns_hold_ps"] is None
    assert view["unconstrained"] == ["hold"]
    assert view["max_slew_violation_count"] == 1
    # A corner whose value is missing and not INF leaves the side unreported.
    view = pnr_openroad._judged_timing(timing, "", {"setup": ["ss"], "hold": ["ff"]})
    assert view["wns_hold_ps"] is None
    assert view["unconstrained"] == []
    assert pnr_openroad._timing_met(view, ["hold"]) is None


# -- unset, digest -----------------------------------------------------------------


def _flow_record(tmp_path, monkeypatch, signoff):
    backend, artefacts = _repair_backend(
        tmp_path / str(bool(signoff)),
        monkeypatch,
        post_route=_PR,
        corners=["ss", "ff"],
        env=_HOLD_AT_SS,
    )
    if signoff is not None:
        _with_run_signoff(backend, signoff)
    res = backend.run()
    assert res.results["result"] == "PASS", res.results
    text = Path(backend._script_path()).read_text()
    text = text.replace(backend.suite_dir, "<root>")
    text = re.sub(r"\d{8}T\d{6}-\d+", "<run>", text)
    return pnr_checkpoints.RUN_BINDING_RE.sub("", text), res


def test_the_block_adds_one_line_to_pnr_tcl_and_unset_adds_none(tmp_path, monkeypatch):
    for sub in ("True", "False"):
        (tmp_path / sub).mkdir()
    unset, res = _flow_record(tmp_path, monkeypatch, None)
    judged, _ = _flow_record(tmp_path, monkeypatch, {"hold-corners": ["ff"]})

    note = 'puts ">>> Post-route repair: the rounds judge setup at ss ff, hold at ff"\n'
    assert note not in unset
    assert judged.count(note) == 1
    assert judged.replace(note, "") == unset
    assert "signoff" not in res.results


def test_the_block_joins_the_digest_only_when_set(tmp_path):
    platform = _platform(_make_pdk_cfg(tmp_path), sta_corner="typ")
    plain = _make_pnr_cfg(tmp_path, harden=True)
    config = pnr_abstract.abstract_config(plain, platform)
    assert "signoff" not in config

    gated = _make_pnr_cfg(
        tmp_path,
        harden=True,
        overrides=_run(tmp_path, {"fail-on": ["timing"]}).get_overrides(),
    )
    signed = pnr_abstract.abstract_config(gated, platform)
    assert signed["signoff"] == {
        "setup_corners": ["typ"],
        "hold_corners": ["typ"],
        "fail_on": ["timing"],
    }
    assert pnr_abstract.config_digest(signed) != pnr_abstract.config_digest(config)
    assert {k: v for k, v in signed.items() if k != "signoff"} == config

    on_platform = PnrPlatformConfig(
        PnrPlatformConfigFile(
            name="nangate45_typ",
            pdk="nangate45",
            sta_corner="typ",
            signoff={"fail-on": ["timing"]},
        ),
        lambda _name: _make_pdk_cfg(tmp_path),
    )
    assert pnr_abstract.abstract_config(plain, on_platform) == signed
