import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "build_versioned_docs", REPO_ROOT / "scripts/build_versioned_docs.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_latest_stable_tag_ignores_prereleases(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda *args: "v6.42.0rc1\nv6.41.1\nv6.41.0",
    )

    assert MODULE.latest_stable_tag("v6") == "v6.41.1"


def test_latest_stable_tag_requires_exact_release(monkeypatch):
    monkeypatch.setattr(MODULE, "_run", lambda *args: "v6.42.0rc1")

    with pytest.raises(RuntimeError, match="no stable tag"):
        MODULE.latest_stable_tag("v6")


def test_release_dev_snapshot_uses_main_docs_and_sidebar(tmp_path, monkeypatch):
    records = []

    def fake_extract(ref, destination, *paths):
        records.append((ref, paths))
        (destination / "docs").mkdir(parents=True)
        (destination / "sidebars.js").write_text("module.exports = {};")
        return destination

    def fake_build(version, **kwargs):
        records.append(
            (
                version,
                kwargs["docs_dir"],
                kwargs["sidebar_path"],
                kwargs["show_last_update"],
            )
        )

    monkeypatch.setattr(MODULE, "_extract_snapshot", fake_extract)
    monkeypatch.setattr(MODULE, "build_version", fake_build)
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    (tmp_path / "docs").mkdir()

    MODULE.build_all(tmp_path / "builds", ["dev"], dev_ref="origin/main")

    assert records[0] == ("origin/main", ("docs", "sidebars.js"))
    assert records[1][0] == "dev"
    assert records[1][1].name == "docs"
    assert records[1][2].name == "sidebars.js"
    assert records[1][3] is False
