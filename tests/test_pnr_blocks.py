"""`blocks:` — a synthesis or P&R run consuming hardened abstracts (#95).

A block resolves to the `abstract/` a `harden: true` run published. Its
views join the run's own macro lists; anything that cannot be resolved
fails the run before the tool starts, naming the block.
"""

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from rtl_buddy.config.blocks import BlockRef
from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.config.pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from rtl_buddy.config.synth import SynthSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract, pnr_openroad
from rtl_buddy.tools.pnr_abstract import BlockResolutionError, resolve_block

_TECH = "TECH LEF\n"
_CORNER = "library (typ) {}\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _block_platform(tmp_path):
    """The platform the block was hardened on; also what the stale check
    rebuilds the block's config against."""
    pdk = PdkConfig(
        PdkConfigFile(
            name="p",
            site="core",
            corners={"typ": "pdk/typ.lib"},
            tech_lef="pdk/tech.lef",
            macro_lef="pdk/cells.lef",
            tie_hi="T/Z",
            tie_lo="T/Z",
            fill_cells=["F"],
        ),
        str(tmp_path / "top/root_config.yaml"),
    )
    return PnrPlatformConfig(
        PnrPlatformConfigFile(name="p", pdk="p", cts_buffer="B"), lambda _n: pdk
    )


def _block_suite(tmp_path, *, harden=True, module="blk_top", manifest=True, views=True):
    """A block pnr.yaml whose `blk_pnr` run published a current abstract."""
    suite = _write(
        tmp_path / "blk/pnr.yaml",
        dedent(f"""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: blk_pnr
            desc: block
            synth: s
            synth-path: synth.yaml
            platform: p
            harden: {"true" if harden else "false"}
        """),
    )
    out = tmp_path / "blk/artefacts/blk_pnr/abstract"
    out.mkdir(parents=True)
    if views:
        for view in ("lef", "lib", "gds"):
            _write(out / f"{module}.{view}", f"{view}\n")
    if manifest:
        run_cfg = PnrSuiteConfig(str(suite)).get_runs("blk_pnr")[0]
        config = pnr_abstract.abstract_config(run_cfg, _block_platform(tmp_path))
        sdc = _write(tmp_path / "blk/blk.sdc", "create_clock\n")
        _write(
            out / "abstract.manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "block": module,
                    "technology": {
                        "tech_lef": {"path": "tech.lef", "sha256": _sha(_TECH)},
                        "liberty": {"path": "typ.lib", "sha256": _sha(_CORNER)},
                    },
                    "inputs": {
                        "sdc": {"path": str(sdc), "sha256": _sha("create_clock\n")}
                    },
                    "config": {**config, "sha256": pnr_abstract.config_digest(config)},
                    "outputs": {
                        view: {
                            "path": str(out / f"{module}.{view}"),
                            "sha256": _sha(f"{view}\n"),
                        }
                        for view in ("lef", "lib", "gds")
                    }
                    if views
                    else {},
                }
            ),
        )
    return suite


def _ref(suite, name="blk_top", run="blk_pnr"):
    return BlockRef(name=name, pnr_run=run, pnr_suite_path=str(suite))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _top_suite(tmp_path, blocks_yaml):
    path = _write(
        tmp_path / "top/pnr.yaml",
        "rtl-buddy-filetype: pnr_config\nruns:\n"
        "  - name: top_pnr\n    desc: top\n    synth: s\n"
        "    synth-path: synth.yaml\n    platform: p\n" + blocks_yaml,
    )
    return path


def test_a_pnr_block_without_pnr_path_refers_to_its_own_file(tmp_path):
    path = _top_suite(
        tmp_path,
        "    blocks:\n      - {name: a, pnr: a_pnr}\n"
        "      - {name: b, pnr: b_pnr, pnr-path: ../blk/pnr.yaml}\n",
    )
    blocks = PnrSuiteConfig(str(path)).get_runs("top_pnr")[0].get_blocks()
    assert blocks == [
        BlockRef("a", "a_pnr", str(path)),
        BlockRef("b", "b_pnr", str(tmp_path / "blk/pnr.yaml")),
    ]


def test_no_blocks_is_an_empty_list(tmp_path):
    path = _top_suite(tmp_path, "")
    assert PnrSuiteConfig(str(path)).get_runs("top_pnr")[0].get_blocks() == []


@pytest.mark.parametrize(
    "blocks_yaml,message",
    [
        (
            "    blocks:\n      - {name: a, pnr: x}\n      - {name: a, pnr: y}\n",
            "listed twice",
        ),
        ("    blocks:\n      - {name: a, pnr: top_pnr}\n", "lists itself"),
    ],
)
def test_bad_pnr_blocks_are_config_errors(tmp_path, blocks_yaml, message):
    path = _top_suite(tmp_path, blocks_yaml)
    with pytest.raises(FatalRtlBuddyError, match=message):
        PnrSuiteConfig(str(path))


def _synth_suite(tmp_path, blocks_yaml):
    _write(
        tmp_path / "syn/models.yaml",
        'rtl-buddy-filetype: model_config\nmodels:\n  - name: "top"\n'
        "    filelist: []\n",
    )
    return _write(
        tmp_path / "syn/synth.yaml",
        "rtl-buddy-filetype: synth_config\nsyntheses:\n"
        "  - name: top_synth\n    desc: top\n    model: top\n"
        "    model_path: models.yaml\n    tool: yosys\n" + blocks_yaml,
    )


def test_a_synth_block_needs_a_pnr_path(tmp_path):
    path = _synth_suite(tmp_path, "    blocks:\n      - {name: a, pnr: a_pnr}\n")
    with pytest.raises(FatalRtlBuddyError, match="pnr-path"):
        SynthSuiteConfig(str(path))


def test_a_synth_block_resolves_its_pnr_path_against_synth_yaml(tmp_path):
    path = _synth_suite(
        tmp_path,
        "    blocks:\n      - {name: a, pnr: a_pnr, pnr-path: ../blk/pnr.yaml}\n",
    )
    synth = SynthSuiteConfig(str(path)).get_syntheses("top_synth")[0]
    assert synth.get_blocks() == [
        BlockRef("a", "a_pnr", str(tmp_path / "blk/pnr.yaml"))
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_published_abstract_resolves_to_its_three_views(tmp_path):
    block = resolve_block(_ref(_block_suite(tmp_path)))
    out = tmp_path / "blk/artefacts/blk_pnr/abstract"
    assert (block.lef, block.lib, block.gds) == (
        str(out / "blk_top.lef"),
        str(out / "blk_top.lib"),
        str(out / "blk_top.gds"),
    )
    assert block.result_row()["manifest"] == str(out / "abstract.manifest.json")


@pytest.mark.parametrize(
    "kwargs,ref_kwargs,message",
    [
        ({"manifest": False}, {}, "rb pnr blk_pnr -c"),
        ({"harden": False}, {}, "does not set harden: true"),
        ({"module": "other"}, {}, "of module 'other', not 'blk_top'"),
        ({"views": False}, {}, "is missing blk_top.lef, blk_top.lib, blk_top.gds"),
        ({}, {"run": "nope"}, "no such run"),
    ],
)
def test_an_unusable_block_fails_fast_and_says_why(
    tmp_path, kwargs, ref_kwargs, message
):
    suite = _block_suite(tmp_path, **kwargs)
    with pytest.raises(BlockResolutionError, match=message) as info:
        resolve_block(_ref(suite, **ref_kwargs))
    assert "'blk_top'" in str(info.value)


def test_a_missing_pnr_path_fails_fast(tmp_path):
    with pytest.raises(BlockResolutionError, match="pnr-path does not exist"):
        resolve_block(_ref(tmp_path / "absent/pnr.yaml"))


def test_technology_is_compared_by_content(tmp_path):
    block = resolve_block(_ref(_block_suite(tmp_path)))
    same = _write(tmp_path / "top/pdk/typ.lib", _CORNER)
    pnr_abstract.check_technology(block, liberty=str(same), tech_lef=None)
    other = _write(tmp_path / "top/pdk/ss.lib", "library (ss) {}\n")
    with pytest.raises(BlockResolutionError, match="platform/corner mismatch"):
        pnr_abstract.check_technology(block, liberty=str(other), tech_lef=None)


def test_an_abstract_that_records_no_technology_is_refused(tmp_path):
    block = resolve_block(_ref(_block_suite(tmp_path)))
    del block.manifest["technology"]
    lib = _write(tmp_path / "top/pdk/typ.lib", _CORNER)
    with pytest.raises(BlockResolutionError, match="records no corner Liberty"):
        pnr_abstract.check_technology(block, liberty=str(lib), tech_lef=None)


# ---------------------------------------------------------------------------
# The P&R backend
# ---------------------------------------------------------------------------


def _pdk(tmp_path):
    pdk = PdkConfig(
        PdkConfigFile(
            name="p",
            site="core",
            corners={"typ": "pdk/typ.lib"},
            tech_lef="pdk/tech.lef",
            macro_lef="pdk/cells.lef",
            tie_hi="T/Z",
            tie_lo="T/Z",
            fill_cells=["F"],
        ),
        str(tmp_path / "top/root_config.yaml"),
    )
    _write(Path(pdk.get_tech_lef()), _TECH)
    _write(Path(pdk.get_macro_lef()), "")
    _write(tmp_path / "top/pdk/typ.lib", _CORNER)
    return pdk


def _top_backend(tmp_path, monkeypatch, blocks_yaml):
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    _write(
        tmp_path / "top/models.yaml",
        'rtl-buddy-filetype: model_config\nmodels:\n  - name: "top"\n'
        "    filelist: []\n",
    )
    _write(
        tmp_path / "top/synth.yaml",
        "rtl-buddy-filetype: synth_config\nsyntheses:\n  - name: s\n"
        "    desc: s\n    model: top\n    model_path: models.yaml\n"
        "    tool: yosys\n",
    )
    _write(tmp_path / "top/c.sdc", "")
    suite = _top_suite(tmp_path, "    constraints: c.sdc\n" + blocks_yaml)
    pnr_cfg = PnrSuiteConfig(str(suite)).get_runs("top_pnr")[0]
    pdk = _pdk(tmp_path)
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = PnrPlatformConfig(
        PnrPlatformConfigFile(name="p", pdk="p", cts_buffer="B"), lambda _n: pdk
    )
    monkeypatch.setattr(pnr_openroad, "task_status", lambda *a, **k: nullcontext())
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    backend = OpenRoadPnr(
        name="top", pnr_cfg=pnr_cfg, suite_dir=str(suite.parent), root_cfg=root_cfg
    )
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    launched = []

    def _run(cmd, **_kw):
        launched.append(cmd)
        Path(cmd[cmd.index("-log") + 1]).write_text("")
        result = MagicMock(returncode=0, stdout="", stderr="")
        return result

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _run)
    return backend, launched


_BLOCK_YAML = (
    "    blocks:\n      - {name: blk_top, pnr: blk_pnr, pnr-path: ../blk/pnr.yaml}\n"
)


def test_a_top_run_reads_every_block_view_and_reports_the_block(tmp_path, monkeypatch):
    _block_suite(tmp_path)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)

    res = backend.run()

    assert res.is_pass(), res.results
    out = tmp_path / "blk/artefacts/blk_pnr/abstract"
    script = Path(backend._script_path()).read_text()
    assert f"read_lef     {out}/blk_top.lef" in script
    assert f"read_liberty {out}/blk_top.lib" in script
    assert str(out / "blk_top.gds") in backend.pnr_cfg.get_gds_paths()
    [row] = res.results["blocks"]
    assert {k: row[k] for k in ("name", "pnr_run", "pnr_path", "abstract_dir")} == {
        "name": "blk_top",
        "pnr_run": "blk_pnr",
        "pnr_path": str(tmp_path / "blk/pnr.yaml"),
        "abstract_dir": str(out),
    }
    assert row["manifest"] == str(out / "abstract.manifest.json")
    assert row["stale"] is False and row["changes"] == []
    assert row["fingerprints"]["lib"]["sha256"] == _sha("lib\n")


def test_a_block_with_no_abstract_fails_before_openroad(tmp_path, monkeypatch):
    _block_suite(tmp_path, manifest=False)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "rb pnr blk_pnr" in res.results["desc"]
    assert launched == []


def test_a_block_hardened_at_another_corner_fails_before_openroad(
    tmp_path, monkeypatch
):
    _block_suite(tmp_path)
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    (tmp_path / "top/pdk/typ.lib").write_text("library (ss) {}\n")

    res = backend.run()

    assert "platform/corner mismatch" in res.results["desc"]
    assert launched == []


def test_a_run_without_blocks_renders_the_flow_unchanged(tmp_path, monkeypatch):
    backend, _ = _top_backend(tmp_path, monkeypatch, "")
    assert backend.run().is_pass()
    assert "blocks" not in backend.run().results
    assert "abstract" not in Path(backend._script_path()).read_text()


# ---------------------------------------------------------------------------
# The synthesis runner
# ---------------------------------------------------------------------------


def _synth_runner(tmp_path, blocks_yaml):
    from rtl_buddy.runner.synth_runner import SynthRunner

    path = _synth_suite(tmp_path, blocks_yaml)
    synth_cfg = SynthSuiteConfig(str(path)).get_syntheses("top_synth")[0]
    root_cfg = MagicMock()
    root_cfg.get_synth_effort_cfg.return_value.get_openroad_run.return_value = False
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    return SynthRunner(
        name="top_synth",
        root_cfg=root_cfg,
        synth_cfg=synth_cfg,
        suite_dir=str(path.parent),
    )


def test_synthesis_takes_each_block_liberty_and_lef(tmp_path, monkeypatch):
    from rtl_buddy.tools import synth_yosys

    _block_suite(tmp_path)
    seen = {}

    def _fake_run(self):
        seen["lib"] = self.synth_cfg.get_lib_paths()
        seen["lef"] = self.synth_cfg.get_lef_paths()
        return MagicMock(results={"result": "PASS", "desc": None})

    monkeypatch.setattr(synth_yosys.YosysSynth, "run", _fake_run)
    runner = _synth_runner(
        tmp_path,
        "    blocks:\n      - {name: blk_top, pnr: blk_pnr, pnr-path: ../blk/pnr.yaml}\n",
    )

    res = runner.run()
    assert res.results["blocks"][0]["name"] == "blk_top"
    assert res.results["desc"] is None
    out = tmp_path / "blk/artefacts/blk_pnr/abstract"
    assert seen == {
        "lib": [str(out / "blk_top.lib")],
        "lef": [str(out / "blk_top.lef")],
    }


def test_synthesis_with_an_unusable_block_withdraws_its_netlist(tmp_path, monkeypatch):
    from rtl_buddy.tools import synth_yosys

    _block_suite(tmp_path, manifest=False)
    monkeypatch.setattr(
        synth_yosys.YosysSynth, "run", lambda self: pytest.fail("synthesis ran")
    )
    runner = _synth_runner(
        tmp_path,
        "    blocks:\n      - {name: blk_top, pnr: blk_pnr, pnr-path: ../blk/pnr.yaml}\n",
    )
    stale = _write(tmp_path / "syn/artefacts/top_synth/synth_netlist.v", "old\n")

    res = runner.run()

    assert res.results["fail_stage"] == "setup"
    assert "rb pnr blk_pnr" in res.results["desc"]
    assert not stale.exists()


# ---------------------------------------------------------------------------
# Staleness (step 3)
# ---------------------------------------------------------------------------


def test_editing_a_block_input_refuses_the_top_run_naming_the_block(
    tmp_path, monkeypatch
):
    """Acceptance 4: edit a block's SDC, re-run only the top — refused."""
    _block_suite(tmp_path)
    (tmp_path / "blk/blk.sdc").write_text("create_clock -period 5\n")
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    desc = res.results["desc"]
    assert "block 'blk_top' is stale" in desc
    assert f"sdc {tmp_path / 'blk/blk.sdc'} changed" in desc
    assert "rb pnr blk_pnr" in desc and "--accept-stale" in desc
    assert launched == []


def test_a_touched_but_unchanged_input_is_not_stale(tmp_path, monkeypatch):
    """Content, not timestamps: a checkout that rewrites mtimes is current."""
    import os

    _block_suite(tmp_path)
    os.utime(tmp_path / "blk/blk.sdc", (1, 1))
    backend, _ = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    assert backend.run().is_pass()


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda t: (t / "blk/blk.sdc").unlink(), "is gone"),
        (
            lambda t: (t / "blk/artefacts/blk_pnr/abstract/blk_top.lib").write_text(
                "hand edited\n"
            ),
            "abstract lib",
        ),
        (
            lambda t: (t / "blk/pnr.yaml").write_text(
                (t / "blk/pnr.yaml").read_text() + "    floorplan: {utilization: 0.3}\n"
            ),
            "config changed (floorplan)",
        ),
    ],
    ids=["input-deleted", "view-edited", "floorplan-edited"],
)
def test_every_kind_of_change_makes_a_block_stale(tmp_path, mutate, expected):
    suite = _block_suite(tmp_path)
    mutate(tmp_path)
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    block = resolve_block(_ref(suite))

    changes = pnr_abstract.block_changes(block, root_cfg)

    assert any(expected in c for c in changes), changes


def test_accept_stale_runs_the_top_and_qualifies_the_result(tmp_path, monkeypatch):
    _block_suite(tmp_path)
    (tmp_path / "blk/blk.sdc").write_text("create_clock -period 5\n")
    backend, launched = _top_backend(tmp_path, monkeypatch, _BLOCK_YAML)
    backend.accept_stale = True

    res = backend.run()

    assert res.is_pass()
    assert launched
    assert "stale block abstract(s) accepted: blk_top" in res.results["desc"]
    [row] = res.results["blocks"]
    assert row["stale"] is True
    assert any("sdc" in c for c in row["changes"])


def test_every_stale_block_is_counted_in_the_refusal(tmp_path):
    suite = _block_suite(tmp_path)
    (tmp_path / "blk/blk.sdc").write_text("changed\n")
    block = resolve_block(_ref(suite))
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = _block_platform(tmp_path)
    with pytest.raises(BlockResolutionError, match=r"and 1 more stale block"):
        pnr_abstract.assess_blocks([block, block], root_cfg, accept_stale=False)


def test_synthesis_refuses_a_stale_block_unless_accepted(tmp_path, monkeypatch):
    from rtl_buddy.tools import synth_yosys

    _block_suite(tmp_path)
    (tmp_path / "blk/blk.sdc").write_text("changed\n")
    monkeypatch.setattr(
        synth_yosys.YosysSynth,
        "run",
        lambda self: MagicMock(results={"result": "PASS", "desc": "ok"}),
    )
    blocks_yaml = "    blocks:\n      - {name: blk_top, pnr: blk_pnr, pnr-path: ../blk/pnr.yaml}\n"

    refused = _synth_runner(tmp_path, blocks_yaml).run()
    assert refused.results["fail_stage"] == "setup"
    assert "is stale" in refused.results["desc"]

    runner = _synth_runner(tmp_path, blocks_yaml)
    runner.accept_stale = True
    accepted = runner.run()
    assert accepted.results["desc"] == "ok; stale block abstract(s) accepted: blk_top"
    assert accepted.results["blocks"][0]["stale"] is True


def test_rb_pnr_and_rb_synth_take_accept_stale():
    import typer

    from rtl_buddy.rtl_buddy import RtlBuddy

    group = typer.main.get_command(RtlBuddy(name="test_pnr_blocks").app)
    for command in ("pnr", "synth"):
        opts = {o for p in group.commands[command].params for o in p.opts}
        assert "--accept-stale" in opts, command
