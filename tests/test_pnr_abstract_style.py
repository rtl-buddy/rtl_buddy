"""Obstruction style of a hardened block's abstract LEF: `abstract.bloat` and `abstract.feedthrough-layers` (#768)."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.pnr import PnrAbstract, PnrSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.pnr_results import PnrFailResults, PnrPassResults
from rtl_buddy.tools import pnr_abstract, pnr_openroad
from rtl_buddy.tools.pnr_abstract import resolve_block

from test_pnr_blocks import _block_platform, _block_suite, _ref
from test_pnr_harden import _backend, _fake_tools

_BASE_LEF = """\
MACRO demo_top
  PIN a
  END a
  OBS
    LAYER met1 ;
     RECT  0 0 10 10 ;
    LAYER met3 ;
     RECT  0 0 10 10 ;
    LAYER met4 ;
     RECT  0 0 10 10 ;
  END
END demo_top
"""

_SHAPES_LEF = """\
MACRO demo_top
  PIN a
  END a
  OBS
    LAYER met1 ;
     RECT  1 1 2 2 ;
    LAYER met3 ;
     RECT  3 3 4 4 ;
    LAYER met4 ;
     RECT  0 1 10 2 ;
     RECT  0 8 10 9 ;
    LAYER met5 ;
     RECT  5 5 6 6 ;
  END
END demo_top
"""


def _suite(tmp_path, extra=""):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        "rtl-buddy-filetype: pnr_config\nruns:\n"
        "  - name: blk\n    desc: block\n    synth: s\n"
        "    synth-path: synth.yaml\n    floorplan: {utilization: 0.55}\n"
        "    platform: p\n" + extra
    )
    return PnrSuiteConfig(str(path)).get_runs("blk")[0]


def _events(caplog, event):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


# Configuration


def test_abstract_defaults_to_occupied_with_no_feedthrough(tmp_path):
    abstract = _suite(tmp_path, "    harden: true\n").get_abstract()
    assert abstract == PnrAbstract()
    assert abstract.bloat == "occupied" and abstract.is_default()


def test_abstract_parses(tmp_path):
    run = _suite(
        tmp_path,
        "    harden: true\n"
        "    abstract: {bloat: 2, feedthrough-layers: [met4, met5]}\n",
    )
    assert run.get_abstract() == PnrAbstract(
        bloat=2.0, feedthrough_layers=("met4", "met5")
    )


@pytest.mark.parametrize(
    "extra, match",
    [
        ("    abstract: {bloat: 0}\n", "needs harden: true"),
        ("    harden: true\n    abstract: {bloat: -1}\n", "non-negative"),
        ("    harden: true\n    abstract: {bloat: loose}\n", "non-negative"),
        ("    harden: true\n    abstract: {bloat: true}\n", "non-negative"),
        (
            "    harden: true\n    abstract: {feedthrough-layers: met4}\n",
            "list of layer names",
        ),
        (
            "    harden: true\n    abstract: {feedthrough-layers: [met4, met4]}\n",
            "repeats",
        ),
        (
            "    harden: true\n    abstract: {feedthrough-layers: ['met 4']}\n",
            "not a layer name",
        ),
    ],
)
def test_bad_abstract_settings_are_config_errors(tmp_path, extra, match):
    with pytest.raises(FatalRtlBuddyError, match=match):
        _suite(tmp_path, extra)


# Flow Tcl


def _render_with(tmp_path, abstract):
    from test_pnr_harden import _pdk, _platform, _pnr_cfg
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    pnr_cfg = _pnr_cfg(tmp_path, harden=True, abstract=abstract)
    synth = MagicMock()
    synth.get_top.return_value = "demo_top"
    pnr_cfg.resolve_synth_cfg = MagicMock(return_value=synth)
    backend = OpenRoadPnr(
        name="d", pnr_cfg=pnr_cfg, suite_dir=str(tmp_path), root_cfg=MagicMock()
    )
    platform = _platform(_pdk(tmp_path))
    return Path(backend._write_script(platform, pnr_cfg.get_floorplan())).read_text()


def test_the_default_style_writes_the_flow_unchanged():
    assert pnr_abstract.harden_tcl(PnrAbstract()) == pnr_abstract.harden_tcl()
    assert "write_abstract_lef -bloat_occupied_layers" in pnr_abstract.harden_tcl()


def test_a_numeric_bloat_is_a_bloat_factor(tmp_path):
    text = _render_with(tmp_path, PnrAbstract(bloat=2.5))
    staged = f"$OUT_DIR/{pnr_abstract.ABSTRACT_STAGING_NAME}"
    assert f"write_abstract_lef -bloat_factor 2.5 {staged}/${{DESIGN}}.lef" in text
    assert "-bloat_occupied_layers" not in text
    assert "feedthrough.lef" not in text


def test_feedthrough_layers_write_a_real_shape_lef_too(tmp_path):
    text = _render_with(tmp_path, PnrAbstract(feedthrough_layers=("met4",)))
    staged = f"$OUT_DIR/{pnr_abstract.ABSTRACT_STAGING_NAME}"
    base = text.index(
        f"write_abstract_lef -bloat_occupied_layers {staged}/${{DESIGN}}.lef"
    )
    shapes = text.index(
        f"write_abstract_lef -bloat_factor 0 {staged}/${{DESIGN}}.feedthrough.lef"
    )
    assert base < shapes < text.index("write_timing_model")


# Splicing


def test_splice_takes_real_shapes_on_feedthrough_layers_only(tmp_path):
    base = tmp_path / "b.lef"
    shapes = tmp_path / "s.lef"
    base.write_text(_BASE_LEF)
    shapes.write_text(_SHAPES_LEF)

    unused = pnr_abstract.splice_feedthrough_obs(
        str(base), str(shapes), ("met4", "met5", "metal9")
    )

    assert unused == ["metal9"]
    assert not shapes.exists()
    assert base.read_text() == (
        "MACRO demo_top\n  PIN a\n  END a\n  OBS\n"
        "    LAYER met1 ;\n     RECT  0 0 10 10 ;\n"
        "    LAYER met3 ;\n     RECT  0 0 10 10 ;\n"
        "    LAYER met4 ;\n     RECT  0 1 10 2 ;\n     RECT  0 8 10 9 ;\n"
        "    LAYER met5 ;\n     RECT  5 5 6 6 ;\n"
        "  END\nEND demo_top\n"
    )


def test_a_feedthrough_layer_without_shapes_loses_its_obstruction(tmp_path):
    base = tmp_path / "b.lef"
    shapes = tmp_path / "s.lef"
    base.write_text(_BASE_LEF)
    shapes.write_text(_SHAPES_LEF.replace("    LAYER met4 ;\n", "    LAYER met9 ;\n"))
    pnr_abstract.splice_feedthrough_obs(str(base), str(shapes), ("met4",))
    assert "LAYER met4" not in base.read_text()
    assert "LAYER met9" not in base.read_text()


# Publishing


def _with_feedthrough_lef(monkeypatch, backend, text=_SHAPES_LEF):
    staging = Path(backend.artefact_dir) / pnr_abstract.ABSTRACT_STAGING_NAME
    inner = pnr_openroad.subprocess.run

    def _run(cmd, **kwargs):
        result = inner(cmd, **kwargs)
        if hasattr(kwargs.get("stdout"), "write"):
            (staging / "demo_top.lef").write_text(_BASE_LEF)
            if text is not None:
                (staging / "demo_top.feedthrough.lef").write_text(text)
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)


def test_a_feedthrough_run_publishes_the_spliced_lef(tmp_path, monkeypatch, caplog):
    backend = _backend(
        tmp_path,
        monkeypatch,
        pnr_overrides={"abstract": PnrAbstract(feedthrough_layers=("met4", "metal4"))},
    )
    _fake_tools(backend, monkeypatch)
    _with_feedthrough_lef(monkeypatch, backend)

    with caplog.at_level(logging.WARNING):
        res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    out = Path(backend.artefact_dir) / "abstract"
    assert sorted(p.name for p in out.iterdir()) == [
        "abstract.manifest.json",
        "demo_top.gds",
        "demo_top.lef",
        "demo_top.lib",
    ]
    lef = (out / "demo_top.lef").read_text()
    assert "RECT  0 1 10 2 ;" in lef and "LAYER met1 ;\n     RECT  0 0 10 10" in lef
    manifest = json.loads((out / "abstract.manifest.json").read_text())
    import hashlib

    assert (
        manifest["outputs"]["lef"]["sha256"] == hashlib.sha256(lef.encode()).hexdigest()
    )
    assert manifest["config"]["abstract"] == {
        "bloat": "occupied",
        "feedthrough_layers": ["met4", "metal4"],
    }
    [rec] = _events(caplog, "pnr.abstract_feedthrough_layers_unused")
    assert rec.rtl_fields["layers"] == ["metal4"]


def test_a_missing_feedthrough_lef_fails_the_abstract(tmp_path, monkeypatch):
    backend = _backend(
        tmp_path,
        monkeypatch,
        pnr_overrides={"abstract": PnrAbstract(feedthrough_layers=("met4",))},
    )
    _fake_tools(backend, monkeypatch)
    _with_feedthrough_lef(monkeypatch, backend, text=None)

    res = backend.run()

    assert isinstance(res, PnrFailResults)
    assert res.results["fail_stage"] == "abstract"
    assert "demo_top.feedthrough.lef" in res.results["desc"]
    assert not (Path(backend.artefact_dir) / "abstract").exists()


# Staleness of `blocks:`


def _changes(tmp_path, suite):
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    return pnr_abstract.block_changes(resolve_block(_ref(suite)), root_cfg)


def test_an_explicit_default_style_is_not_stale(tmp_path):
    """Abstracts hardened before `abstract:` existed stay current, and spelling out the default changes nothing."""
    suite = _block_suite(tmp_path)
    run = PnrSuiteConfig(str(suite)).get_runs("blk_pnr")[0]
    assert "abstract" not in pnr_abstract.abstract_config(
        run, _block_platform(tmp_path)
    )
    suite.write_text(suite.read_text() + "    abstract: {bloat: occupied}\n")
    assert _changes(tmp_path, suite) == []


@pytest.mark.parametrize(
    "setting",
    ["{bloat: 0}", "{feedthrough-layers: [met4]}"],
)
def test_changing_the_style_makes_the_block_stale(tmp_path, setting):
    suite = _block_suite(tmp_path)
    suite.write_text(suite.read_text() + f"    abstract: {setting}\n")
    assert any("config changed (abstract)" in c for c in _changes(tmp_path, suite))
