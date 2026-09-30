"""Contract gates for the pywellen dependency.

Exercises every pywellen call the consumers use against a real VCD (no mocks), checks
that `pyproject.toml` and the lockfile keep a two-sided pywellen requirement matching
`pywellen_compat.SUPPORTED_SPECIFIER`, and watches pywellen streaming for panics.
"""

from __future__ import annotations

import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

import pywellen

from rtl_buddy.tools import pywellen_compat


_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

# Covers a 1-bit signal, a multi-bit bus, a partially-z bus value, a late first change,
# an x state and a nested scope.
_VCD = """\
$timescale 10ps $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 4 # bus $end
$scope module sub $end
$var wire 1 $ rst $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0000 #
#10
1!
b0011 #
x$
#20
0!
bzz11 #
1$
#30
1!
"""


@pytest.fixture
def vcd(tmp_path: Path) -> Path:
    path = tmp_path / "api.vcd"
    path.write_text(_VCD)
    return path


@pytest.fixture
def wf(vcd: Path):
    return pywellen.Waveform(str(vcd))


def test_required_api_surface_is_present():
    """`pywellen_compat.missing_api` reports nothing missing on the installed pywellen."""
    assert pywellen_compat.missing_api(pywellen) == []


def test_waveform_path_lookup_returns_a_var(wf):
    var = wf["top.clk"]
    assert isinstance(var, pywellen.Var)
    assert var.name == "clk"
    assert var.full_name == "top.clk"


def test_waveform_path_lookup_misses_raise_key_error(wf):
    """`WaveformValueReader` treats KeyError, and only KeyError, as a
    signal-not-in-the-dump miss.
    """
    with pytest.raises(KeyError):
        wf["top.no_such_signal"]


def test_waveform_scope_lookup_returns_a_scope(wf):
    """`get_scope_signals` resolves a scope through the same ``wf[path]``."""
    scope = wf["top.sub"]
    assert scope.name == "sub"
    assert scope.full_name == "top.sub"
    assert [v.name for v in scope.vars()] == ["rst"]


def test_scope_walk_yields_children_and_vars(wf):
    """`rb saif` walks ``wf.scopes()``, ``scope.scopes()`` and ``scope.vars()``."""
    (top,) = list(wf.scopes())
    assert top.name == "top"
    assert top.full_name == "top"
    assert [v.name for v in top.vars()] == ["clk", "bus"]
    (sub,) = list(top.scopes())
    assert sub.full_name == "top.sub"


def test_var_getters_are_zero_arg(wf):
    """These calls take no hierarchy argument."""
    bus = wf["top.bus"]
    assert bus.name == "bus"
    assert bus.full_name == "top.bus"
    assert bus.bitwidth == 4
    assert bus.var_type == "Wire"
    assert bus.signal is not None


def test_signal_value_at_change_and_between_changes(wf):
    """A value can be read at an arbitrary time, not only at edges."""
    clk = wf["top.clk"].signal
    assert clk.value_at(0) == 0
    assert clk.value_at(5) == 0
    assert clk.value_at(10) == 1
    assert clk.value_at(15) == 1
    assert clk.value_at(30) == 1
    assert clk.value_at(999) == 1


def test_signal_value_before_first_change_is_none(wf):
    """A time before the first change reads None, which the value reader shows as a
    blank annotation.
    """
    rst = wf["top.sub.rst"].signal
    assert rst.value_at(0) is None
    assert rst.value_at(9) is None
    assert rst.value_at(10) == "x"


def test_signal_change_vector_shape_and_value_types(wf):
    """``sig[:]`` is a time-ordered list of ``(int time, value)`` that `rb saif`
    accumulates over.

    The value is an ``int`` for a fully 2-state sample and a ``str`` (MSB first) when
    any bit is x or z.
    """
    clk = wf["top.clk"].signal
    assert clk[:] == [(0, 0), (10, 1), (20, 0), (30, 1)]
    assert len(clk) == 4
    assert all(isinstance(t, int) for t, _ in clk[:])

    bus = wf["top.bus"].signal
    assert bus[:] == [(0, 0), (10, 3), (20, "zz11")]
    assert isinstance(bus[:][1][1], int)
    assert isinstance(bus[:][2][1], str)

    rst = wf["top.sub.rst"].signal
    assert rst[:] == [(10, "x"), (20, 1)]


def test_timescale_shape(wf):
    """`rb saif` emits ``(TIMESCALE <int factor> <lowercased unit>)``."""
    ts = wf.timescale
    assert int(ts.factor) == 10
    assert str(ts.unit).lower() == "ps"


def _pywellen_requirement() -> Requirement:
    pyproject = tomllib.loads(_PYPROJECT.read_text())
    reqs = [Requirement(s) for s in pyproject["project"]["dependencies"]]
    (req,) = [r for r in reqs if r.name == "pywellen"]
    return req


def test_pyproject_pin_is_two_sided_and_matches_the_guard():
    """The requirement has a floor and a cap, matching the runtime guard's error
    message.
    """
    req = _pywellen_requirement()
    floors = [s for s in req.specifier if s.operator == ">="]
    caps = [s for s in req.specifier if s.operator == "<"]
    assert floors and caps, (
        f"pywellen must keep a two-sided pin, got '{req.specifier}' — an "
        "unbounded pin is what broke rb wave and rb saif in #263"
    )
    (floor,) = floors
    (cap,) = caps
    assert Version(floor.version) >= Version(pywellen_compat.MIN_VERSION)
    assert Version(cap.version) <= Version(pywellen_compat.MAX_VERSION_EXCLUSIVE)
    assert Requirement(f"pywellen{pywellen_compat.SUPPORTED_SPECIFIER}")


def test_guard_message_names_the_version_and_the_range(monkeypatch):
    """The guard's message includes the installed version and the supported range."""
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.logging_utils import _human_message

    captured: dict = {}
    monkeypatch.setattr(
        pywellen_compat,
        "log_event",
        lambda _logger, _level, _event, **fields: captured.update(fields),
    )
    monkeypatch.setattr(pywellen_compat, "pywellen_version", lambda: "0.24.2")
    fake = type("module", (), {"Waveform": type("Waveform", (), {})})
    monkeypatch.setitem(sys.modules, "pywellen", fake)

    with pytest.raises(FatalRtlBuddyError):
        pywellen_compat.require_random_access_api("rb wave")

    message = _human_message("pywellen.api_missing", captured)
    assert "0.24.2" in message
    assert pywellen_compat.SUPPORTED_SPECIFIER in message
    assert "rb wave" in message


def test_installed_pywellen_satisfies_the_pin():
    """The lockfile pins a pywellen inside the declared range."""
    version = metadata.version("pywellen")
    req = _pywellen_requirement()
    assert req.specifier.contains(version), (
        f"installed pywellen {version} is outside the declared pin "
        f"'{req.specifier}' — relock, or fix the pin (#263, #268)"
    )
    assert pywellen_compat.pywellen_version() == version


class TestStreamingCanary:
    """pywellen streaming must not panic on the inputs `rb saif` would give it.

    Each case opens its own Waveform because ``stream_changes`` consumes the reader.
    """

    @staticmethod
    def _stream(path: Path, include) -> list:
        seen: list = []
        wave = pywellen.Waveform(str(path))
        if include == "last":
            include = [list(wave.all_vars())[-1]]
        wave.stream_changes(lambda *change: seen.append(change), include)
        return seen

    def test_streaming_all_signals_does_not_panic(self, vcd):
        # include=None streams every signal.
        assert len(self._stream(vcd, None)) == 9

    def test_streaming_the_last_declared_signal_does_not_panic(self, vcd):
        # The highest signal id. The callback receives (time, SignalId, value); only the
        # count matters.
        assert len(self._stream(vcd, "last")) == 2

    def test_streaming_a_single_signal_file_does_not_panic(self, tmp_path):
        one = tmp_path / "one.vcd"
        one.write_text(
            "$timescale 1ns $end\n"
            "$scope module top $end\n"
            "$var wire 1 ! clk $end\n"
            "$upscope $end\n"
            "$enddefinitions $end\n"
            "#0\n0!\n#5\n1!\n"
        )
        assert len(self._stream(one, None)) == 2

    def test_streaming_time_steps_does_not_panic(self, vcd):
        seen: list = []
        wave = pywellen.Waveform(str(vcd))
        wave.stream_time_steps(lambda *step: seen.append(step), None)
        assert [step[0] for step in seen] == [0, 10, 20, 30]
