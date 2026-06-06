"""Tests for ``rb hub`` view.json builder.

The real generation invokes ``rtl-buddy-view`` (the external viewer
binary) which we don't want to require in CI. We mock the
``RtlBuddyView`` subprocess wrapper, so these tests only pin the
plumbing: cache layout, executable-missing error path, and exit-code
handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.hub import view_builder


def _model(tmp_path: Path) -> ModelConfig:
    return ModelConfig(name="demo", filelist=[], path=str(tmp_path / "models.yaml"))


def test_cache_dir_under_rtl_buddy_subdir(tmp_path):
    assert view_builder.cache_dir(tmp_path) == tmp_path / ".rtl-buddy" / "cache"


def test_view_json_path_stable_per_model(tmp_path):
    assert (
        view_builder.view_json_path(tmp_path, "demo")
        == tmp_path / ".rtl-buddy" / "cache" / "view-demo.json"
    )


def test_build_view_json_missing_viewer_binary_raises(tmp_path, monkeypatch):
    """``rtl-buddy-view`` not on PATH → fatal error at hub start, not
    at first HTTP request."""
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: None)
    with pytest.raises(FatalRtlBuddyError, match="not found on PATH"):
        view_builder.build_view_json(project_root=tmp_path, model_cfg=_model(tmp_path))


def test_build_view_json_success_writes_to_stable_path(tmp_path, monkeypatch):
    """When the subprocess wrapper exits 0 and writes the JSON, the
    builder returns the stable cache path."""
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.artefact_dir = str(tmp_path / "artefacts" / "hier" / "demo")
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)

        def run(self) -> int:
            # Pretend rtl-buddy-view wrote the file.
            out = Path(captured["kwargs"]["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"schema_version": "1.0", "top": "demo", "nodes": []}')
            return 0

    monkeypatch.setattr(view_builder, "RtlBuddyView", FakeRunner)

    result = view_builder.build_view_json(
        project_root=tmp_path, model_cfg=_model(tmp_path)
    )
    assert result == view_builder.view_json_path(tmp_path, "demo")
    assert result.is_file()
    # Verify the wrapper got configured for JSON output at the cache
    # path — covers the contract the builder makes with RtlBuddyView.
    assert captured["kwargs"]["format"] == "json"
    assert captured["kwargs"]["output"] == str(result)
    assert captured["kwargs"]["executable"] == "/fake/rtl-buddy-view"


def test_build_view_json_subprocess_failure_raises(tmp_path, monkeypatch):
    """Non-zero exit from rtl-buddy-view → fatal error referencing the
    log file (rtl-buddy-view's hier.log under artefacts/)."""
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    class FailingRunner:
        def __init__(self, **kwargs):
            self.artefact_dir = str(tmp_path / "artefacts" / "hier" / "demo")
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.artefact_dir) / "hier.log").write_text("$ rtl-buddy-view ...\n")

        def run(self) -> int:
            return 7

    monkeypatch.setattr(view_builder, "RtlBuddyView", FailingRunner)

    with pytest.raises(FatalRtlBuddyError, match=r"hier\.log"):
        view_builder.build_view_json(project_root=tmp_path, model_cfg=_model(tmp_path))


def test_build_view_json_creates_cache_dir(tmp_path, monkeypatch):
    """The .rtl-buddy/cache directory may not exist yet on a fresh
    project — the builder should create it."""
    assert not view_builder.cache_dir(tmp_path).exists()
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    class FakeRunner:
        def __init__(self, **kwargs):
            self.artefact_dir = str(tmp_path / "artefacts" / "hier" / "demo")
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)
            self._out = Path(kwargs["output"])

        def run(self) -> int:
            self._out.write_text('{"schema_version": "1.0"}')
            return 0

    monkeypatch.setattr(view_builder, "RtlBuddyView", FakeRunner)
    view_builder.build_view_json(project_root=tmp_path, model_cfg=_model(tmp_path))
    assert view_builder.cache_dir(tmp_path).is_dir()


def test_build_view_json_passes_cdc_annotations_when_back_pointer_set(
    tmp_path, monkeypatch
):
    """When ``model.cdc`` is set, the view builder routes through
    cdc_builder to produce a domain map and feeds the path to
    rtl-buddy-view as ``--cdc-annotations``. Tested at the
    integration boundary by stubbing cdc_builder."""
    from rtl_buddy.hub import cdc_builder

    fake_domain = tmp_path / ".rtl-buddy" / "cache" / "domain-demo.json"
    monkeypatch.setattr(
        cdc_builder,
        "build_domain_map",
        lambda **kwargs: fake_domain,
    )
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.artefact_dir = str(tmp_path / "artefacts" / "hier" / "demo")
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)

        def run(self) -> int:
            out = Path(captured["kwargs"]["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"schema_version": "1.0"}')
            return 0

    monkeypatch.setattr(view_builder, "RtlBuddyView", FakeRunner)

    model = ModelConfig(
        name="demo",
        filelist=[],
        cdc="cdc.yaml",
        path=str(tmp_path / "models.yaml"),
    )
    view_builder.build_view_json(project_root=tmp_path, model_cfg=model)
    assert captured["kwargs"]["cdc_annotations"] == str(fake_domain)


def test_build_view_json_no_cdc_annotations_when_back_pointer_absent(
    tmp_path, monkeypatch
):
    """Without ``model.cdc`` the cdc_builder returns ``None`` and
    rtl-buddy-view runs without ``--cdc-annotations``."""
    from rtl_buddy.hub import cdc_builder

    monkeypatch.setattr(cdc_builder, "build_domain_map", lambda **kwargs: None)
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.artefact_dir = str(tmp_path / "artefacts" / "hier" / "demo")
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)

        def run(self) -> int:
            out = Path(captured["kwargs"]["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"schema_version": "1.0"}')
            return 0

    monkeypatch.setattr(view_builder, "RtlBuddyView", FakeRunner)
    view_builder.build_view_json(project_root=tmp_path, model_cfg=_model(tmp_path))
    assert captured["kwargs"]["cdc_annotations"] is None


# --- TB view (rtl-buddy-view #99 / 6b) -----------------------------------


def _test_cfg(model: ModelConfig, tb_name: str = "tb_basic"):
    """Minimal TestConfig with a TestbenchConfig that has a toplevel."""
    from rtl_buddy.config.test import TestbenchConfig, TestConfig

    tb = TestbenchConfig(name=tb_name, filelist=[], toplevel="tb_top")
    return TestConfig(
        name="t1",
        desc="",
        model=model,
        _reglvl=0,
        pa=None,
        pd=None,
        uvm=None,
        preproc_path=None,
        postproc_path=None,
        sweep_path=None,
        tb=tb,
        timeout=None,
    )


def test_view_json_path_for_tb_keys_on_model_and_tb(tmp_path):
    """Cache key is (model, tb) — two tests sharing the same TB
    share the artefact."""
    assert (
        view_builder.view_json_path_for_tb(tmp_path, "demo", "tb_basic")
        == tmp_path / ".rtl-buddy" / "cache" / "view-demo-tb-tb_basic.json"
    )
    # DUT-view path is untouched.
    assert (
        view_builder.view_json_path(tmp_path, "demo")
        == tmp_path / ".rtl-buddy" / "cache" / "view-demo.json"
    )


def test_build_view_json_tb_mode_uses_tb_cache_path_and_forwards_test_cfg(
    tmp_path, monkeypatch
):
    """When ``test_cfg`` is supplied, the builder writes to the
    (model, tb)-keyed cache file and passes the test_cfg through to
    RtlBuddyView so the wrapper can emit --tb-top + merge filelists."""
    from rtl_buddy.hub import cdc_builder

    monkeypatch.setattr(cdc_builder, "build_domain_map", lambda **kwargs: None)
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.artefact_dir = str(
                tmp_path / "artefacts" / "hier" / "demo" / "tb" / "tb_basic"
            )
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)

        def run(self) -> int:
            out = Path(captured["kwargs"]["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"schema_version": "1.1"}')
            return 0

    monkeypatch.setattr(view_builder, "RtlBuddyView", FakeRunner)

    model = _model(tmp_path)
    test_cfg = _test_cfg(model)
    result = view_builder.build_view_json(
        project_root=tmp_path, model_cfg=model, test_cfg=test_cfg
    )
    assert result == view_builder.view_json_path_for_tb(tmp_path, "demo", "tb_basic")
    assert result.is_file()
    # The wrapper received the test_cfg, so it'll emit --tb-top.
    assert captured["kwargs"]["test_cfg"] is test_cfg


def test_build_view_json_tb_mode_error_message_mentions_test_name(
    tmp_path, monkeypatch
):
    """Failure path surfaces the test name in the error so the user
    can spot which TB-mode click broke."""
    from rtl_buddy.hub import cdc_builder

    monkeypatch.setattr(cdc_builder, "build_domain_map", lambda **kwargs: None)
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")

    class FailingRunner:
        def __init__(self, **kwargs):
            self.artefact_dir = str(
                tmp_path / "artefacts" / "hier" / "demo" / "tb" / "tb_basic"
            )
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.artefact_dir) / "hier.log").write_text("$ rtl-buddy-view ...\n")

        def run(self) -> int:
            return 7

    monkeypatch.setattr(view_builder, "RtlBuddyView", FailingRunner)

    model = _model(tmp_path)
    test_cfg = _test_cfg(model)
    with pytest.raises(FatalRtlBuddyError, match=r"--test t1"):
        view_builder.build_view_json(
            project_root=tmp_path, model_cfg=model, test_cfg=test_cfg
        )


# --- contract floor (view.json schema_version major) ---------------------
#
# rtl_buddy pins no rtl-buddy-view version, so the on-disk view.json shape
# is floored independently by its top-level schema_version major. 1.x is
# forward-compatible (minor bumps add fields only); a breaking major or an
# unparseable value must fail loudly before the SPA loads it.


def _runner_emitting(tmp_path: Path, payload: str):
    """Build a FakeRunner class that writes ``payload`` and exits 0."""

    class FakeRunner:
        def __init__(self, **kwargs):
            self.artefact_dir = str(tmp_path / "artefacts" / "hier" / "demo")
            Path(self.artefact_dir).mkdir(parents=True, exist_ok=True)
            self._out = Path(kwargs["output"])

        def run(self) -> int:
            self._out.parent.mkdir(parents=True, exist_ok=True)
            self._out.write_text(payload)
            return 0

    return FakeRunner


@pytest.mark.parametrize("schema", ["1.0", "1.1", "1.5", "1"])
def test_build_view_json_accepts_supported_schema_major(tmp_path, monkeypatch, schema):
    """Any 1.x view.json passes — minor bumps only add fields."""
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")
    monkeypatch.setattr(
        view_builder,
        "RtlBuddyView",
        _runner_emitting(tmp_path, f'{{"schema_version": "{schema}", "top": "demo"}}'),
    )
    result = view_builder.build_view_json(
        project_root=tmp_path, model_cfg=_model(tmp_path)
    )
    assert result.is_file()


def test_build_view_json_rejects_future_schema_major(tmp_path, monkeypatch):
    """A breaking major (2.x) is rejected with an upgrade hint, even though
    rtl-buddy-view exited 0 — the contract floor is package-independent."""
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")
    monkeypatch.setattr(
        view_builder,
        "RtlBuddyView",
        _runner_emitting(tmp_path, '{"schema_version": "2.0", "top": "demo"}'),
    )
    with pytest.raises(FatalRtlBuddyError, match=r"schema major 2"):
        view_builder.build_view_json(project_root=tmp_path, model_cfg=_model(tmp_path))


def test_build_view_json_rejects_unparseable_schema(tmp_path, monkeypatch):
    """A missing / non-numeric schema_version is a clear error, not a
    confusing KeyError or silent serve of a malformed payload."""
    monkeypatch.setattr(view_builder.shutil, "which", lambda _: "/fake/rtl-buddy-view")
    monkeypatch.setattr(
        view_builder,
        "RtlBuddyView",
        _runner_emitting(tmp_path, '{"top": "demo"}'),
    )
    with pytest.raises(FatalRtlBuddyError, match=r"schema_version unparseable"):
        view_builder.build_view_json(project_root=tmp_path, model_cfg=_model(tmp_path))
