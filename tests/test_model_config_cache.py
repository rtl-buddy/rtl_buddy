"""``models.yaml`` is parsed once per file version, however many tests name its models."""

import pprint
import os
from pathlib import Path

import pytest

from rtl_buddy.config import model as model_mod
from rtl_buddy.config.model import ModelConfigLoader
from rtl_buddy.config.suite import SuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError

N_MODELS = 5
N_TESTS = 20


def _models_yaml(extra: str = "") -> str:
    out = "rtl-buddy-filetype: model_config\nmodels:\n"
    for i in range(N_MODELS):
        out += f'  - name: m{i}\n    desc: "model {i}"\n    filelist:\n      - rtl/m{i}.sv\n'
    return out + extra


def _write_suite(root: Path) -> Path:
    (root / "models.yaml").write_text(_models_yaml())
    lines = [
        "rtl-buddy-filetype: test_config",
        "testbenches:",
        "  - name: tb",
        "    toplevel: tb_top",
        "    filelist:",
        "      - tb_top.sv",
        "tests:",
    ]
    for i in range(N_TESTS):
        lines += [
            f"  - name: t{i}",
            "    desc: test",
            "    reglvl: 0",
            f"    model: m{i % N_MODELS}",
            "    model_path: models.yaml",
            "    testbench: tb",
        ]
    suite = root / "tests.yaml"
    suite.write_text("\n".join(lines) + "\n")
    return suite


class _NoCache(dict):
    def __setitem__(self, key, value):
        pass


@pytest.fixture(autouse=True)
def fresh_cache():
    model_mod._MODEL_CACHE.clear()
    yield
    model_mod._MODEL_CACHE.clear()


@pytest.fixture
def parse_count(monkeypatch):
    calls = []
    real = model_mod.load_yaml

    def counting(text, path=None, **kwargs):
        calls.append(str(path))
        return real(text, path, **kwargs)

    monkeypatch.setattr(model_mod, "load_yaml", counting)
    return calls


def _tests(suite: Path) -> str:
    return pprint.pformat(
        {k: v.model.__dict__ for k, v in SuiteConfig(str(suite)).tests.items()}
    )


def test_suite_parses_shared_models_yaml_once(tmp_path, parse_count):
    suite = _write_suite(tmp_path)
    cfg = SuiteConfig(str(suite))
    assert len(cfg.tests) == N_TESTS
    assert parse_count == [str(tmp_path / "models.yaml")]


def test_cached_suite_matches_uncached(tmp_path, monkeypatch):
    suite = _write_suite(tmp_path)
    cached = _tests(suite)
    monkeypatch.setattr(model_mod, "_MODEL_CACHE", _NoCache())
    assert _tests(suite) == cached


def test_changed_file_is_read_again(tmp_path, parse_count):
    path = tmp_path / "models.yaml"
    path.write_text(_models_yaml())
    assert len(ModelConfigLoader(str(path)).get_models()) == N_MODELS
    path.write_text(_models_yaml("  - name: extra\n    filelist: [x.sv]\n"))
    assert len(ModelConfigLoader(str(path)).get_models()) == N_MODELS + 1
    assert len(parse_count) == 2


def test_same_size_edit_in_the_same_mtime_is_read_again(tmp_path, parse_count):
    path = tmp_path / "models.yaml"
    path.write_text(_models_yaml().replace("m0", "ma"))
    stamp = path.stat().st_mtime_ns
    ModelConfigLoader(str(path))
    path.write_text(_models_yaml().replace("m0", "mb"))
    os.utime(path, ns=(stamp, stamp))
    assert ModelConfigLoader(str(path)).get_model("mb")
    assert len(parse_count) == 2


def test_loaders_do_not_share_model_objects(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(_models_yaml())
    first = ModelConfigLoader(str(path)).get_model("m0")
    first.filelist.append("changed.sv")
    first.desc = "changed"
    second = ModelConfigLoader(str(path)).get_model("m0")
    assert second.filelist == ["rtl/m0.sv"]
    assert second.desc == "model 0"


def test_model_path_follows_the_spelling_given(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(_models_yaml())
    (tmp_path / "sub").mkdir()
    other = str(tmp_path / "sub" / ".." / "models.yaml")
    assert ModelConfigLoader(str(path)).get_model("m0").path == str(path)
    assert ModelConfigLoader(other).get_model("m0").path == other


def test_loader_returns_one_object_per_model(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(_models_yaml())
    loader = ModelConfigLoader(str(path))
    assert loader.get_model("m1") is loader.get_models()[1]


def test_failed_load_is_not_cached(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "rtl-buddy-filetype: model_config\nmodels:\n  - name: a\n  - name: a\n"
    )
    for _ in range(2):
        with pytest.raises(FatalRtlBuddyError):
            ModelConfigLoader(str(path))
    assert not model_mod._MODEL_CACHE


def test_duplicate_key_still_rejected(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "rtl-buddy-filetype: model_config\nmodels:\n"
        "  - name: a\n    filelist: [x.sv]\n    filelist: [y.sv]\n"
    )
    with pytest.raises(FatalRtlBuddyError) as err:
        ModelConfigLoader(str(path))
    assert "duplicate key 'filelist'" in str(err.value.__cause__)
