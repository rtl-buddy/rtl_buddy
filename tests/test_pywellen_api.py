"""Contract gates for the pywellen dependency (#263, #267).

pywellen is pre-1.0 and rewrites its public API on minor bumps: 0.25.0
removed the whole random-access ``Waveform`` surface 0.20-0.24 exposed, which
blanked ``rb wave`` annotations and crashed ``rb saif`` in the field before
anyone noticed (#263). Three gates keep that from recurring, all of them
loud rather than skipped:

* the **API contract** — every attribute and call shape
  ``tools/surfer_wcp.py`` and ``tools/saif_from_trace.py`` use, exercised
  against a real VCD written by the test, never a mock. A mock would happily
  model an API pywellen no longer has, which is the exact failure being
  guarded.
* the **pin** — ``pyproject.toml`` must keep a two-sided pywellen
  requirement matching ``tools/pywellen_compat.SUPPORTED_SPECIFIER``, and the
  resolved version must satisfy it, so loosening the pin or a lockfile
  drifting out of range fails here instead of in the field.
* the **streaming canary** — ``rb saif`` does not stream yet (#269), but the
  upstream panic that blocks that migration is cheap to watch for, so a
  regression stays visible.

pywellen is deliberately *not* in ``tool_manifest.MANIFEST``: it is a core
wheel dependency, not an optional tool a user may lack (see the note beside
that module's python-extras block).
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

# Small enough to read at a glance, wide enough to pin every value shape the
# consumers meet: a 1-bit signal, a multi-bit bus, a partially-z bus value, a
# signal whose first change is late (so there is a "before first change"
# time), an x state, and a nested scope.
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


# ---------------------------------------------------------------------------
# API contract — real trace, no mocks
# ---------------------------------------------------------------------------


def test_required_api_surface_is_present():
    """The import-time guard's own table, checked against the real module.

    ``pywellen_compat.missing_api`` is what turns an out-of-range pywellen
    into a FatalRtlBuddyError at runtime; if its table drifts from what the
    installed pywellen offers, the guard passes and the consumer still dies.
    """
    assert pywellen_compat.missing_api(pywellen) == []


def test_waveform_path_lookup_returns_a_var(wf):
    var = wf["top.clk"]
    assert isinstance(var, pywellen.Var)
    assert var.name == "clk"
    assert var.full_name == "top.clk"


def test_waveform_path_lookup_misses_raise_key_error(wf):
    """`WaveformValueReader` treats KeyError — and only KeyError — as a
    signal-not-in-the-dump miss; anything else is logged as an API break."""
    with pytest.raises(KeyError):
        wf["top.no_such_signal"]


def test_waveform_scope_lookup_returns_a_scope(wf):
    """`get_scope_signals` resolves a scope through the same ``wf[path]``."""
    scope = wf["top.sub"]
    assert scope.name == "sub"
    assert scope.full_name == "top.sub"
    assert [v.name for v in scope.vars()] == ["rst"]


def test_scope_walk_yields_children_and_vars(wf):
    """`rb saif` walks ``wf.scopes()`` -> ``scope.scopes()`` -> ``scope.vars()``."""
    (top,) = list(wf.scopes())
    assert top.name == "top"
    assert top.full_name == "top"
    assert [v.name for v in top.vars()] == ["clk", "bus"]
    (sub,) = list(top.scopes())
    assert sub.full_name == "top.sub"


def test_var_getters_are_zero_arg(wf):
    """All of these took a hierarchy argument before 0.25."""
    bus = wf["top.bus"]
    assert bus.name == "bus"
    assert bus.full_name == "top.bus"
    assert bus.bitwidth == 4
    # `_iter_vars` compares var_type against the literal "Parameter".
    assert bus.var_type == "Wire"
    assert bus.signal is not None


def test_signal_value_at_change_and_between_changes(wf):
    """`rb wave` annotates at an arbitrary cursor time, not only at edges."""
    clk = wf["top.clk"].signal
    assert clk.value_at(0) == 0  # at the first change
    assert clk.value_at(5) == 0  # between changes: holds the previous value
    assert clk.value_at(10) == 1  # at a change
    assert clk.value_at(15) == 1
    assert clk.value_at(30) == 1  # at the last change
    assert clk.value_at(999) == 1  # after the last change: holds


def test_signal_value_before_first_change_is_none(wf):
    """The quiet miss the value reader converts to a blank annotation —
    distinct from an API break, which is logged at ERROR."""
    rst = wf["top.sub.rst"].signal
    assert rst.value_at(0) is None
    assert rst.value_at(9) is None
    assert rst.value_at(10) == "x"


def test_signal_change_vector_shape_and_value_types(wf):
    """``sig[:]`` is what `rb saif` accumulates T0/T1/TX/TZ/TC over.

    `_bit_stats` depends on the exact shape: a time-ordered list of
    ``(int time, value)``, with a value that is an ``int`` for a fully
    2-state sample (shifted per bit) and a ``str`` when any bit is x or z
    (scanned per character, LSB last).
    """
    clk = wf["top.clk"].signal
    assert clk[:] == [(0, 0), (10, 1), (20, 0), (30, 1)]
    assert len(clk) == 4
    assert all(isinstance(t, int) for t, _ in clk[:])

    bus = wf["top.bus"].signal
    assert bus[:] == [(0, 0), (10, 3), (20, "zz11")]
    assert isinstance(bus[:][1][1], int)  # multi-bit, 2-state -> int
    assert isinstance(bus[:][2][1], str)  # any x/z -> str, MSB first

    rst = wf["top.sub.rst"].signal
    assert rst[:] == [(10, "x"), (20, 1)]


def test_timescale_shape(wf):
    """`rb saif` emits ``(TIMESCALE <int factor> <lowercased unit>)``."""
    ts = wf.timescale
    assert int(ts.factor) == 10
    assert str(ts.unit).lower() == "ps"


# ---------------------------------------------------------------------------
# Pin consistency
# ---------------------------------------------------------------------------


def _pywellen_requirement() -> Requirement:
    pyproject = tomllib.loads(_PYPROJECT.read_text())
    reqs = [Requirement(s) for s in pyproject["project"]["dependencies"]]
    (req,) = [r for r in reqs if r.name == "pywellen"]
    return req


def test_pyproject_pin_is_two_sided_and_matches_the_guard():
    """Parse rather than string-compare: the invariant is "this floor, this
    cap", not the author's whitespace.

    The cap is the root-cause fix for #263 — without it the next pre-1.0
    rewrite resolves into a wheel install and breaks `rb wave` / `rb saif`
    silently. The floor guarantees the released upstream streaming fix. The
    runtime guard repeats both in its error message, so they must agree.
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
    # The guard's message tells the user what to reinstall; it must be true.
    assert Requirement(f"pywellen{pywellen_compat.SUPPORTED_SPECIFIER}")


def test_guard_message_names_the_version_and_the_range(monkeypatch):
    """Closes the loop between the guard's log fields and the human message.

    The human-mode line is assembled from ``fields``, so dropping one from the
    ``log_event`` call would print ``pywellenNone`` to a user who is already
    staring at a broken install.
    """
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
    """Catches a lockfile that drifted out of the declared range — the
    lock is what CI and every `uv sync` actually install (#263, #268)."""
    version = metadata.version("pywellen")
    req = _pywellen_requirement()
    assert req.specifier.contains(version), (
        f"installed pywellen {version} is outside the declared pin "
        f"'{req.specifier}' — relock, or fix the pin (#263, #268)"
    )
    assert pywellen_compat.pywellen_version() == version


# ---------------------------------------------------------------------------
# Streaming canary (#269) — not a consumer, a watch
# ---------------------------------------------------------------------------


class TestStreamingCanary:
    """`rb saif` scans with the getter API, not streaming — deliberately.

    Streaming is the better fit for a single-pass accumulation and is tracked
    in #269, but pywellen 0.25.x streaming panicked (`index out of bounds`,
    a `SignalToVarMap` off-by-one) on exactly these three inputs until the
    upstream fix landed in 0.25.3. Nothing here is on a production path; the
    point is that a regression in the release we pin stays visible instead of
    surfacing when someone picks up #269.

    Each case opens its own Waveform: ``stream_changes`` consumes the reader,
    so a second call on the same object silently yields nothing.
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
        # include=None: the all-signals case, which panicked pre-0.25.3.
        assert len(self._stream(vcd, None)) == 9

    def test_streaming_the_last_declared_signal_does_not_panic(self, vcd):
        # The highest signal id — the one the off-by-one indexed past the end.
        # Callback shape is (time, SignalId, value); only the count matters.
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
