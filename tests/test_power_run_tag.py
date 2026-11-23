"""Tests for `rb power --run-tag`.

A tagged call writes its power runs under the power suite's `artefacts/.runs/<tag>/` and
locks that tree. A `netlist-source: pnr` run reads the P&R run `rb pnr --run-tag <tag>`
wrote, from a copy taken into the power call's tree under the P&R tree's lock. An untagged
call keeps the flat layout and reads the untagged P&R run.
"""

import contextlib
import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from power_support import _FakePlatform
from typer.testing import CliRunner

from rtl_buddy.artifact_lock import LOCK_FILENAME
from rtl_buddy.config.power import PowerSuiteConfig
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.runner.power_results import PowerPassResults
from rtl_buddy.tools import pnr_tag_inputs, power_openroad
from rtl_buddy.tools.artifact_paths import RUNS_DIRNAME, run_artifact_root
from rtl_buddy.tools.pnr_abstract import ABSTRACT_MANIFEST_NAME
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr
from rtl_buddy.tools.pnr_tag_inputs import (
    CONSUMED_BLOCKS_NAME,
    ROUTED_COPIES_DIRNAME,
    TagInputs,
)
from rtl_buddy.tools.power_openroad import OpenRoadPower

_TOP = "m"
_ODB = f"{_TOP}.routed.odb"
_SDC = f"{_TOP}.routed.sdc"
_SPEF = f"{_TOP}.routed.spef"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _project(root: Path, *, blocks: str = "") -> None:
    """A synthesis, a P&R run `blk_pnr` over it, and a power run `blk_power` reading it, each in its own suite."""
    _write(
        root / "synth/synth.yaml",
        "rtl-buddy-filetype: synth_config\nsyntheses:\n"
        "  - name: s\n    desc: s\n    model: m\n    model_path: models.yaml\n"
        "    tool: yosys\n",
    )
    _write(
        root / "synth/models.yaml",
        "rtl-buddy-filetype: model_config\nmodels:\n  - name: m\n    filelist: []\n",
    )
    _write(
        root / "pnr/pnr.yaml",
        "rtl-buddy-filetype: pnr_config\nruns:\n"
        "  - name: blk_pnr\n    desc: blk_pnr\n    synth: s\n"
        "    synth-path: ../synth/synth.yaml\n    floorplan: {utilization: 0.55}\n"
        "    platform: p\n    reglvl: 0\n" + blocks,
    )
    _write(
        root / "power/power.yaml",
        "rtl-buddy-filetype: power_config\nruns:\n"
        "  - name: blk_power\n    desc: blk_power\n    tool: openroad\n"
        "    mode: static\n    netlist-source: pnr\n    pnr: blk_pnr\n"
        "    pnr-path: ../pnr/pnr.yaml\n    platform: p\n    reglvl: 0\n",
    )


def _routed_run(directory: Path, label: str, *, tagged: bool = True) -> Path:
    """Write a P&R run's routed outputs, each naming `label`, as a finished run leaves them."""
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "pnr.tcl", "write_db x\nwrite_spef x\n")
    _write(directory / _ODB, f"odb {label}\n")
    _write(directory / _SDC, f"create_clock -name {label} -period 1\n")
    _write(directory / _SPEF, f"*SPEF {label}\n")
    if tagged:
        pnr_tag_inputs.mark_attempt(str(directory))
    return directory


def _power_cfg(root: Path):
    return PowerSuiteConfig(str(root / "power/power.yaml")).get_runs("blk_power")[0]


def _backend(root: Path, run_tag: str | None = "t", **kw) -> OpenRoadPower:
    backend = OpenRoadPower(
        name="blk_power/openroad",
        power_cfg=_power_cfg(root),
        suite_dir=str(root / "power"),
        root_cfg=MagicMock(),
        run_tag=run_tag,
        **kw,
    )
    backend._resolve_platform = lambda: _FakePlatform(liberty=str(root / "typ.lib"))
    return backend


def _tagged_pnr_dir(root: Path, tag: str = "t") -> Path:
    return run_artifact_root(root / "pnr", tag) / "blk_pnr"


def _copies(root: Path, tag: str = "t") -> list[Path]:
    copies = run_artifact_root(root / "power", tag) / ROUTED_COPIES_DIRNAME
    return sorted(p for p in copies.iterdir()) if copies.is_dir() else []


def test_a_tagged_run_writes_under_its_tag_and_reads_a_copy_of_the_tagged_pnr_run(
    tmp_path,
):
    _project(tmp_path)
    _routed_run(tmp_path / "pnr/artefacts/blk_pnr", "untagged", tagged=False)
    source = _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    backend = _backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    assert backend.artefact_dir == str(
        tmp_path / "power/artefacts" / RUNS_DIRNAME / "t" / "blk_power"
    )
    [copy] = _copies(tmp_path)
    assert copy.name.startswith("blk_pnr-")
    for name in (_ODB, _SDC, _SPEF, "pnr.tcl"):
        assert (copy / name).read_bytes() == (source / name).read_bytes()
        # Modification times come along, so the SPEF is judged against its own pnr.tcl.
        assert (copy / name).stat().st_mtime_ns == (source / name).stat().st_mtime_ns
    assert f"read_db {copy / _ODB}" in script
    assert f"read_sdc {copy / _SDC}" in script
    assert f"read_spef {copy / _SPEF}" in script
    assert "untagged" not in "".join(p.read_text() for p in copy.iterdir())
    assert backend._script_inputs["pnr_dir"] == str(source)


def test_an_untagged_run_reads_the_untagged_pnr_run_in_place(tmp_path):
    _project(tmp_path)
    flat = _routed_run(tmp_path / "pnr/artefacts/blk_pnr", "untagged", tagged=False)
    _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    backend = _backend(tmp_path, run_tag=None)

    script = Path(backend._write_script()).read_text()

    assert backend.artefact_dir == str(tmp_path / "power/artefacts/blk_power")
    assert f"read_db {flat / _ODB}" in script
    assert "pnr_dir" not in backend._script_inputs
    assert not (tmp_path / "power/artefacts" / RUNS_DIRNAME).exists()


def test_a_tag_that_never_ran_the_pnr_run_fails_at_setup_naming_the_command(
    tmp_path,
):
    _project(tmp_path)
    # An untagged run is there, but a tagged call never falls back to it.
    _routed_run(tmp_path / "pnr/artefacts/blk_pnr", "untagged", tagged=False)

    result = _backend(tmp_path).run()

    assert result.results["result"] == "FAIL"
    assert result.results["fail_stage"] == "setup"
    desc = result.results["desc"]
    assert "pnr run 'blk_pnr' has not run under --run-tag t" in desc
    assert f"`rb pnr blk_pnr -c {tmp_path / 'pnr/pnr.yaml'} --run-tag t`" in desc
    assert _copies(tmp_path) == []


def test_a_tagged_pnr_run_without_a_routed_odb_names_its_own_directory(tmp_path):
    _project(tmp_path)
    source = _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    (source / _ODB).unlink()

    result = _backend(tmp_path).run()

    desc = result.results["desc"]
    assert result.results["fail_stage"] == "setup"
    assert f"routed ODB not found at {source / _ODB}" in desc
    assert "--run-tag t`" in desc


def test_the_copy_is_taken_under_the_pnr_trees_lock(tmp_path, monkeypatch):
    _project(tmp_path)
    _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    held = []
    real = pnr_tag_inputs.tree_lock

    @contextlib.contextmanager
    def _recording(root, **kw):
        with real(root, **kw):
            held.append(Path(root))
            yield
            held.append(f"released {_copies(tmp_path)!r}")

    monkeypatch.setattr(pnr_tag_inputs, "tree_lock", _recording)

    _backend(tmp_path)._write_script()

    pnr_tree = run_artifact_root(tmp_path / "pnr", "t")
    # The copy was in place before the P&R tree was let go.
    assert held == [pnr_tree, f"released {_copies(tmp_path)!r}"]
    assert len(_copies(tmp_path)) == 1


def test_a_copy_that_does_not_match_its_source_is_refused(tmp_path, monkeypatch):
    _project(tmp_path)
    _routed_run(_tagged_pnr_dir(tmp_path), "tagged")

    def _bad_copy(src, dst, **kw):
        Path(dst).write_text("torn\n")

    monkeypatch.setattr(pnr_tag_inputs.shutil, "copy2", _bad_copy)

    result = _backend(tmp_path).run()

    assert result.results["fail_stage"] == "setup"
    assert "does not match it" in result.results["desc"]
    assert _copies(tmp_path) == []


def test_a_reused_copy_whose_files_changed_is_refused(tmp_path):
    _project(tmp_path)
    _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    _backend(tmp_path)._write_script()
    [copy] = _copies(tmp_path)
    (copy / _ODB).write_text("edited\n")

    result = _backend(tmp_path).run()

    desc = result.results["desc"]
    assert result.results["fail_stage"] == "setup"
    assert f"the {_ODB} of the copy {copy} has changed" in desc
    assert f"remove {copy}" in desc


def test_a_rerun_pnr_run_is_copied_anew_and_the_earlier_copy_kept(tmp_path):
    _project(tmp_path)
    source = _routed_run(_tagged_pnr_dir(tmp_path), "first")
    _backend(tmp_path)._write_script()
    _routed_run(source, "second")

    script = Path(_backend(tmp_path)._write_script()).read_text()

    first, second = sorted(_copies(tmp_path), key=lambda p: (p / _ODB).read_text())
    assert (first / _ODB).read_text() == "odb first\n"
    assert (second / _ODB).read_text() == "odb second\n"
    assert f"read_db {second / _ODB}" in script


@pytest.mark.parametrize(
    ("first", "then", "parasitics"),
    [
        pytest.param((10, 20), (30, 20), "estimated", id="spef-now-older"),
        pytest.param((30, 20), (10, 20), "spef", id="spef-now-newer"),
    ],
)
def test_a_reused_copy_judges_the_spef_by_its_sources_times(
    tmp_path, first, then, parasitics
):
    """The same bytes restored with the other time order reuse the copy, whose own times are the first call's."""
    _project(tmp_path)
    source = _routed_run(_tagged_pnr_dir(tmp_path), "tagged")

    def _times(script_s, spef_s):
        os.utime(source / "pnr.tcl", ns=(script_s * 10**9, script_s * 10**9))
        os.utime(source / _SPEF, ns=(spef_s * 10**9, spef_s * 10**9))

    _times(*first)
    _backend(tmp_path)._write_script()
    _times(*then)
    backend = _backend(tmp_path)

    script = Path(backend._write_script()).read_text()

    [copy] = _copies(tmp_path)
    assert (copy / "pnr.tcl").stat().st_mtime_ns == first[0] * 10**9
    assert backend._parasitics == parasitics
    assert (f"read_spef {copy / _SPEF}" in script) == (parasitics == "spef")


def test_one_call_copies_a_pnr_run_once(tmp_path, monkeypatch):
    _project(tmp_path)
    _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    inputs = TagInputs.for_suite(tmp_path / "power", "t")
    copies = []
    real = shutil.copy2
    monkeypatch.setattr(
        pnr_tag_inputs.shutil,
        "copy2",
        lambda src, dst, **kw: copies.append(Path(src).name) or real(src, dst, **kw),
    )

    _backend(tmp_path, tag_inputs=inputs)._write_script()
    _backend(tmp_path, tag_inputs=inputs)._write_script()

    assert sorted(copies) == sorted([_ODB, _SDC, _SPEF, "pnr.tcl"])


def test_the_session_hooks_run_in_the_tagged_pnr_runs_own_directory(
    tmp_path, monkeypatch
):
    hook = _write(tmp_path / "pnr/session.tcl", "set_load 0 [all_outputs]\n")
    _project(tmp_path, blocks=f"    hooks: {{session: [{hook}]}}\n")
    source = _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    seen = {}

    def _prelude(pnr_cfg, platform, pnr_dir, out_dir):
        seen.update(pnr_dir=pnr_dir, out_dir=out_dir)
        return "# hooks\n"

    monkeypatch.setattr(power_openroad, "session_hooks_prelude", _prelude)
    monkeypatch.setattr(power_openroad, "run_platform", lambda *a: None)
    backend = _backend(tmp_path)

    backend._write_script()

    assert seen == {"pnr_dir": str(source), "out_dir": backend.artefact_dir}


def _publish_block_copy(root: Path, tag: str = "t") -> Path:
    """An abstract copy of block `blk` in the P&R suite's tag tree, as a tagged `rb pnr` leaves it."""
    _write(
        root / "blk/pnr.yaml",
        "rtl-buddy-filetype: pnr_config\nruns:\n"
        "  - name: blk_pnr\n    desc: blk\n    synth: s\n"
        "    synth-path: ../synth/synth.yaml\n    floorplan: {utilization: 0.55}\n"
        "    platform: p\n    harden: true\n",
    )
    copy = (
        run_artifact_root(root / "pnr", tag)
        / pnr_tag_inputs.BLOCK_COPIES_DIRNAME
        / "blk_pnr-0-0"
    )
    _write(
        copy / ABSTRACT_MANIFEST_NAME,
        json.dumps({"schema_version": 1, "block": "blk", "outputs": {"gds": None}}),
    )
    _write(copy / "blk.lef", "MACRO blk\n")
    _write(copy / "blk.lib", "library (blk) {}\n")
    return copy


_BLOCKS = "    blocks:\n      - {name: blk, pnr: blk_pnr, pnr-path: ../blk/pnr.yaml}\n"


def test_blocks_resolve_to_the_copies_the_tagged_pnr_run_consumed(tmp_path):
    _project(tmp_path, blocks=_BLOCKS)
    abstract = _publish_block_copy(tmp_path)
    source = _routed_run(_tagged_pnr_dir(tmp_path), "tagged")
    manifest_sha = hashlib.sha256(
        (abstract / ABSTRACT_MANIFEST_NAME).read_bytes()
    ).hexdigest()
    _write(
        source / CONSUMED_BLOCKS_NAME,
        json.dumps(
            {
                "run_tag": "t",
                "blocks": [
                    {
                        "name": "blk",
                        "pnr": "blk_pnr",
                        "run_tag": "t",
                        "manifest_sha256": manifest_sha,
                        "pnr_path": "blk/pnr.yaml",
                        "abstract_dir": os.path.relpath(abstract, tmp_path),
                    }
                ],
            }
        ),
    )
    _write(tmp_path / "root_config.yaml", "rtl-buddy-filetype: project_root_config\n")
    backend = _backend(tmp_path)

    backend._write_script()

    [block] = backend._blocks
    assert block.abstract_dir == str(abstract)
    assert block.run_tag == "t"
    [copy] = _copies(tmp_path)
    assert (copy / CONSUMED_BLOCKS_NAME).is_file()


def test_blocks_the_tagged_pnr_run_recorded_none_of_fail_at_setup(tmp_path):
    _project(tmp_path, blocks=_BLOCKS)
    _publish_block_copy(tmp_path)
    _routed_run(_tagged_pnr_dir(tmp_path), "tagged")

    result = _backend(tmp_path).run()

    desc = result.results["desc"]
    assert result.results["fail_stage"] == "setup"
    assert "upstream block 'blk'" in desc
    assert "records no abstract for it — re-run the P&R run under --run-tag t" in desc


def test_phys_run_is_refused_under_a_tag(tmp_path):
    _project(tmp_path)
    cfg = _power_cfg(tmp_path)
    cfg.netlist_source = "synth"
    cfg.phys_run = "s"
    backend = OpenRoadPower(
        name="blk_power/openroad",
        power_cfg=cfg,
        suite_dir=str(tmp_path / "power"),
        root_cfg=MagicMock(),
        run_tag="t",
    )

    with pytest.raises(RuntimeError, match="does not write — run it without --run-tag"):
        backend._bind_phys_dir()
    assert backend.phys_dir == backend.artefact_dir


# CLI: a tagged `rb pnr`, then a tagged `rb power` that reads it.


@pytest.fixture
def cli_project(minimal_project, monkeypatch):
    """The minimal project with the three suites; OpenROAD is replaced on both flows.

    The P&R stand-in writes routed outputs naming the tag it ran under. The power stand-in
    records the inputs it resolved and passes.
    """
    _project(minimal_project)
    root_config = minimal_project / "root_config.yaml"
    root_config.write_text(
        root_config.read_text()
        + "\ncfg-power-tools:\n  - name: openroad\n    tool: openroad\n"
    )

    def _pnr(self):
        _routed_run(
            Path(self.artefact_dir),
            self.run_tag or "untagged",
            tagged=bool(self.run_tag),
        )
        return PnrPassResults(name=f"{self.pnr_cfg.get_name()}/results")

    def _power(self):
        inputs = self._resolve_inputs()
        _write(
            Path(self.artefact_dir) / "inputs.json",
            json.dumps({k: inputs.get(k) for k in ("odb", "sdc", "pnr_dir")}),
        )
        return PowerPassResults(name=f"{self.power_cfg.get_name()}/results")

    monkeypatch.setattr(OpenRoadPnr, "run", _pnr)
    monkeypatch.setattr(OpenRoadPower, "run", _power)
    return minimal_project


def _rb(*args):
    rb = RtlBuddy(name="test_power_run_tag")
    try:
        return CliRunner().invoke(rb.app, ["--machine", *args])
    finally:
        rb._artifact_locks.release_all()


def test_rb_power_reads_the_pnr_run_its_tag_wrote(cli_project):
    for tag in ("a", "b"):
        result = _rb("pnr", "blk_pnr", "-c", "pnr/pnr.yaml", "--run-tag", tag)
        assert result.exit_code == 0, result.output

    result = _rb("power", "blk_power", "-c", "power/power.yaml", "--run-tag", "a")

    assert result.exit_code == 0, result.output
    tagged = cli_project / "power/artefacts" / RUNS_DIRNAME / "a"
    inputs = json.loads((tagged / "blk_power/inputs.json").read_text())
    [copy] = _copies(cli_project, "a")
    assert inputs["odb"] == str(copy / _ODB)
    assert (copy / _ODB).read_text() == "odb a\n"
    assert inputs["pnr_dir"] == str(_tagged_pnr_dir(cli_project, "a"))
    assert (tagged / LOCK_FILENAME).is_file()
    assert (tagged / "rtl_buddy.log").is_file()
    assert not (cli_project / "power/artefacts/blk_power").exists()
    assert not (cli_project / "power/rtl_buddy.log").exists()


def test_rb_power_without_a_tag_is_unchanged(cli_project):
    assert _rb("pnr", "blk_pnr", "-c", "pnr/pnr.yaml").exit_code == 0

    result = _rb("power", "blk_power", "-c", "power/power.yaml")

    assert result.exit_code == 0, result.output
    inputs = json.loads(
        (cli_project / "power/artefacts/blk_power/inputs.json").read_text()
    )
    assert inputs == {
        "odb": str(cli_project / "pnr/artefacts/blk_pnr" / _ODB),
        "sdc": str(cli_project / "pnr/artefacts/blk_pnr" / _SDC),
        "pnr_dir": None,
    }
    assert not (cli_project / "power/artefacts" / RUNS_DIRNAME).exists()


def test_rb_power_copies_before_opening_its_log_and_rejects_a_bad_tag(cli_project):
    result = _rb("power", "blk_power", "-c", "power/power.yaml", "--run-tag", "../x")

    assert result.exit_code != 0
    assert "not a safe path segment" in str(result.exception)
    assert not (cli_project / "power/artefacts").exists()
