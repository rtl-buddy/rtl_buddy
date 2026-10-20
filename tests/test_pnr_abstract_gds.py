"""A hardened block's abstract without a GDS: `abstract: {gds: false}` (rtl_buddy#869).

Pinned behaviour:

- the key parses, defaults to a GDS, takes only a boolean and is in the config digest
  only when false, so existing digests do not change;
- such a run needs no stream-out: it publishes the LEF, Liberty and manifest, with the
  manifest's GDS output null;
- a parent P&Rs against the abstract with no block GDS in its stream-out inputs;
- a `gds-mode: strict` stream-out of the parent (`--gds`, its own `harden` or
  `rb pnr-export`) fails at setup naming the block;
- a `gds-mode: preview` stream-out, `--png` included, treats the block as a cell
  without GDS: the run or export passes with the qualifier naming the block.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from rtl_buddy.config.pnr import GdsMode, PnrAbstract
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.tools import pnr_abstract, pnr_openroad
from rtl_buddy.tools.pnr_abstract import resolve_block
from rtl_buddy.tools.pnr_openroad import GDS_INCOMPLETE, GdsExport

from test_pnr_abstract_style import _suite
from test_pnr_blocks import (
    _BLOCK_YAML,
    _block_platform,
    _block_suite,
    _ref,
    _top_backend,
)
from test_pnr_harden import _backend, _fake_tools


def test_abstract_gds_defaults_on_and_parses_off(tmp_path):
    assert _suite(tmp_path, "    harden: true\n").get_abstract().gds is True
    abstract = _suite(tmp_path, "    harden: true\n    abstract: {gds: false}\n")
    assert abstract.get_abstract() == PnrAbstract(gds=False)
    assert not abstract.get_abstract().is_default()


@pytest.mark.parametrize("value", ["maybe", "'false'", "0", "[false]"])
def test_abstract_gds_takes_only_a_boolean(tmp_path, value):
    with pytest.raises(FatalRtlBuddyError, match="gds"):
        _suite(tmp_path, f"    harden: true\n    abstract: {{gds: {value}}}\n")


def test_abstract_gds_is_in_the_config_digest_only_when_false(tmp_path):
    platform = _block_platform(tmp_path)
    plain = _suite(tmp_path, "    harden: true\n    abstract: {gds: true}\n")
    assert "abstract" not in pnr_abstract.abstract_config(plain, platform)
    bloated = _suite(tmp_path, "    harden: true\n    abstract: {bloat: 0}\n")
    assert "gds" not in pnr_abstract.abstract_config(bloated, platform)["abstract"]
    off = _suite(tmp_path, "    harden: true\n    abstract: {gds: false}\n")
    assert pnr_abstract.abstract_config(off, platform)["abstract"]["gds"] is False


def test_a_gds_less_harden_does_not_stream_out(tmp_path, monkeypatch):
    backend = _backend(
        tmp_path, monkeypatch, pnr_overrides={"abstract": PnrAbstract(gds=False)}
    )
    assert backend.emit_gds is False
    assert backend.gds_mode == GdsMode.PREVIEW


def test_a_gds_less_harden_publishes_the_lef_lib_and_a_manifest_without_gds(
    tmp_path, monkeypatch
):
    backend = _backend(
        tmp_path, monkeypatch, pnr_overrides={"abstract": PnrAbstract(gds=False)}
    )
    calls = _fake_tools(backend, monkeypatch)

    res = backend.run()

    assert isinstance(res, PnrPassResults), res.results
    assert len(calls) == 1, "KLayout ran"
    out = Path(backend.artefact_dir) / "abstract"
    assert sorted(p.name for p in out.iterdir()) == [
        "abstract.manifest.json",
        "demo_top.lef",
        "demo_top.lib",
    ]
    manifest = json.loads((out / "abstract.manifest.json").read_text())
    assert manifest["outputs"]["gds"] is None
    assert manifest["outputs"]["lib"]["path"].endswith("abstract/demo_top.lib")
    assert manifest["config"]["abstract"]["gds"] is False


def test_a_gds_less_abstract_resolves_without_a_gds(tmp_path):
    block = resolve_block(_ref(_block_suite(tmp_path, gds=False)))

    assert block.gds is None
    assert block.lef.endswith("blk_top.lef") and block.lib.endswith("blk_top.lib")
    assert block.result_row()["fingerprints"]["gds"] is None


def test_an_abstract_that_lost_its_gds_is_still_refused(tmp_path):
    _block_suite(tmp_path)
    (tmp_path / "blk/artefacts/blk_pnr/abstract/blk_top.gds").unlink()

    with pytest.raises(pnr_abstract.BlockResolutionError, match="missing blk_top.gds"):
        resolve_block(_ref(tmp_path / "blk/pnr.yaml"))


def test_a_parent_routes_against_a_gds_less_block(tmp_path, monkeypatch):
    _block_suite(tmp_path, gds=False)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)

    res = backend.run()

    assert res.is_pass(), res.results
    assert launched
    assert not any("blk_top" in p for p in backend.pnr_cfg.get_gds_paths())


def test_a_strict_parent_stream_out_fails_at_setup_naming_the_block(
    tmp_path, monkeypatch
):
    _block_suite(tmp_path, gds=False)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    backend.emit_gds = True
    backend.gds_mode = GdsMode.STRICT

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"] == (
        f"block 'blk_top' has no GDS: pnr run 'blk_pnr' in {tmp_path}/blk/pnr.yaml "
        "hardens it with abstract: {gds: false}, and this run's gds-mode: strict "
        "stream-out needs its GDS"
    )
    assert launched == []


def test_a_hardened_parent_fails_at_setup_naming_the_block(tmp_path, monkeypatch):
    _block_suite(tmp_path, gds=False)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    backend = type(backend)(
        name="top",
        pnr_cfg=replace(backend.pnr_cfg, harden=True),
        suite_dir=backend.suite_dir,
        root_cfg=backend.root_cfg,
    )
    assert backend.emit_gds and backend.gds_mode == GdsMode.STRICT
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    monkeypatch.setattr(backend, "_probe_sta_modes", lambda: True)

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "block 'blk_top' has no GDS" in res.results["desc"]
    assert launched == []


def _klayout_without_the_block(backend, monkeypatch):
    """Stream-out as KLayout does it: a block whose GDS is not an input is a cell without layout."""
    streamed = []

    def _def2stream(_platform, design, *, in_def=None):
        streamed.append(list(backend.pnr_cfg.get_gds_paths()))
        gds = Path(backend.artefact_dir) / f"{design}.gds"
        gds.parent.mkdir(parents=True, exist_ok=True)
        gds.write_bytes(b"gds")
        return GdsExport(
            mode=backend.gds_mode,
            status=GDS_INCOMPLETE,
            png_requested=backend.emit_png,
            gds_path=str(gds),
            missing_cells=["blk_top"],
            desc=f"GDS incomplete: no layout for {pnr_openroad.describe_missing_cells(['blk_top'])}",
        )

    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: None)
    monkeypatch.setattr(backend, "_run_def2stream", _def2stream)
    monkeypatch.setattr(
        backend,
        "_run_gds2png",
        lambda _p, _g, design: str(Path(_g).with_suffix(".png")),
    )
    return streamed


@pytest.mark.parametrize("png", [False, True], ids=["gds", "png"])
def test_a_preview_parent_streams_out_without_the_block(tmp_path, monkeypatch, png):
    _block_suite(tmp_path, gds=False)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    backend.emit_gds = True
    backend.emit_png = png
    assert backend.gds_mode == GdsMode.PREVIEW
    streamed = _klayout_without_the_block(backend, monkeypatch)

    res = backend.run()

    assert res.is_pass(), res.results
    assert launched
    assert streamed and not any("blk_top" in p for p in streamed[0])
    assert res.results["desc"].startswith(
        "P&R passed; GDS incomplete: no layout for 1 cell (blk_top);"
    )
    assert res.results["gds_missing_cells"] == ["blk_top"]
    assert (res.results.get("png_path") is not None) == png


def test_pnr_export_of_a_parent_follows_its_gds_mode(tmp_path, monkeypatch):
    _block_suite(tmp_path, gds=False)
    backend, _launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    backend.gds_mode = GdsMode.STRICT

    res = backend.export_only()

    assert res.results["fail_stage"] == "setup"
    assert "block 'blk_top' has no GDS" in res.results["desc"]
    assert "the gds-mode: strict stream-out needs its GDS" in res.results["desc"]

    backend, _launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    assert backend.gds_mode == GdsMode.PREVIEW
    streamed = _klayout_without_the_block(backend, monkeypatch)
    Path(backend.artefact_dir).mkdir(parents=True, exist_ok=True)
    (Path(backend.artefact_dir) / "top.def").write_text("DESIGN top ;\n")

    res = backend.export_only()

    assert res.is_pass(), res.results
    assert streamed
    assert res.results["desc"] == "GDS incomplete: no layout for 1 cell (blk_top)"


def test_a_png_only_re_render_does_not_need_the_block_gds(tmp_path, monkeypatch):
    _block_suite(tmp_path, gds=False)
    backend, _launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    monkeypatch.setattr(pnr_openroad, "_resolve_klayout_exe", lambda: None)

    res = backend.export_only(png_only=True)

    assert "has no GDS" not in (res.results.get("desc") or "")
