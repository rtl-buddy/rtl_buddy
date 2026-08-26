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


def test_versions_to_build_can_exclude_dev_without_hiding_it_from_navigation():
    versions = ["dev", "v6", "v5"]

    assert MODULE.versions_to_build(versions, {"dev"}) == ["v6", "v5"]
    assert versions == ["dev", "v6", "v5"]
