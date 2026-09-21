import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "publish_docusaurus", REPO_ROOT / "scripts/publish_docusaurus.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
publish = MODULE.publish


def _build(path: Path, marker: str) -> Path:
    path.mkdir()
    (path / "index.html").write_text(marker)
    (path / "asset.js").write_text(marker)
    return path


def _pages(path: Path) -> Path:
    path.mkdir()
    (path / ".git").write_text("gitdir: elsewhere")
    (path / "v5").mkdir()
    (path / "v5/index.html").write_text("v5")
    (path / "versions.json").write_text(
        json.dumps(
            [
                {"version": "dev", "title": "dev", "aliases": []},
                {"version": "v5", "title": "v5", "aliases": ["latest"]},
            ]
        )
    )
    return path


def test_publish_preserves_old_versions_and_updates_latest(tmp_path):
    build = _build(tmp_path / "build", "v6")
    pages = _pages(tmp_path / "pages")

    publish(build, pages, version="v6", update_latest=True)

    assert (pages / "v5/index.html").read_text() == "v5"
    assert (pages / "v6/index.html").read_text() == "v6"
    assert (pages / "latest/index.html").read_text() == "v6"
    assert "latest/" in (pages / "index.html").read_text()
    versions = json.loads((pages / "versions.json").read_text())
    assert [item["version"] for item in versions] == ["dev", "v6", "v5"]
    assert next(item for item in versions if item["version"] == "v6")["aliases"] == [
        "latest"
    ]
    assert next(item for item in versions if item["version"] == "v5")["aliases"] == []


def test_publish_dev_does_not_replace_latest(tmp_path):
    build = _build(tmp_path / "build", "dev")
    pages = _pages(tmp_path / "pages")
    (pages / "latest").mkdir()
    (pages / "latest/index.html").write_text("stable")

    publish(build, pages, version="dev", update_latest=False)

    assert (pages / "dev/index.html").read_text() == "dev"
    assert (pages / "latest/index.html").read_text() == "stable"


def test_publish_rejects_unsafe_version(tmp_path):
    build = _build(tmp_path / "build", "bad")
    pages = _pages(tmp_path / "pages")

    with pytest.raises(ValueError, match="invalid documentation version"):
        publish(build, pages, version="../latest", update_latest=True)
