"""`session_hooks_prelude`: what a P&R run's sessions set up around their `session` hooks, for `rb power` to run them the same way."""

from pathlib import Path

import pytest

from test_pnr_blocks import _write
from test_pnr_hook_points import _run_backend
from test_pnr_hooks import _tcl_run

from rtl_buddy.runner.pnr_results import PnrPassResults
from rtl_buddy.tools.pnr_openroad import run_platform, session_hooks_prelude

_PROBE = (
    'puts "HOOK [pwd] $OUT_DIR $SIGNAL_LAYERS $CLOCK_LAYERS $DESIGN [info level]"\n'
)


def _session_run(tmp_path, monkeypatch, hook_text=_PROBE):
    hook = _write(tmp_path / "hooks" / "session.tcl", hook_text)
    backend, artefacts = _run_backend(
        tmp_path,
        monkeypatch,
        {"session": (str(hook),)},
        checkpoints=None,
        platform={"dont_use_cells": ["SLOW_*"]},
    )
    return backend, artefacts, hook


def _prelude(backend, artefacts, out_dir):
    cfg = backend.pnr_cfg
    return session_hooks_prelude(
        cfg, run_platform(cfg, backend.root_cfg), str(artefacts), str(out_dir)
    )


def test_it_repeats_the_p_and_r_session_s_variables_procs_and_hooks(
    tmp_path, monkeypatch
):
    backend, artefacts, hook = _session_run(tmp_path, monkeypatch)
    assert isinstance(backend.run(), PnrPassResults)
    flow = (artefacts / "pnr.tcl").read_text()
    out_dir = tmp_path / "power"

    prelude = _prelude(backend, artefacts, out_dir)

    end = "file mkdir $OUT_DIR\n"
    variables = flow[: flow.index(end) + len(end)]
    assert prelude.startswith(variables.replace(str(artefacts), str(out_dir)))
    for line in (
        "proc rb::hook::run {point path} {",
        "set_dont_use [list SLOW_*]",
        f'rb::hook::run session "{hook}"',
    ):
        assert line in flow
        assert line in prelude
    assert "set_max_fanout" not in prelude
    assert "read_db" not in prelude


def test_its_hooks_see_the_flow_variables_in_the_p_and_r_directory(
    tmp_path, monkeypatch
):
    """`$OUT_DIR` is the caller's; relative paths resolve in the P&R run's directory, and the session returns to its own after."""
    backend, artefacts, _hook = _session_run(tmp_path, monkeypatch)
    out_dir = tmp_path / "power"
    out_dir.mkdir()
    prelude = _prelude(backend, artefacts, out_dir)

    done = _tcl_run(
        tmp_path,
        "proc unknown {args} {}\n"
        + f"cd {out_dir}\n"
        + prelude
        + 'puts "AFTER [pwd]"\n',
    )

    assert done.returncode == 0, done.stderr
    assert (
        f"HOOK {Path(artefacts).resolve()} {out_dir} metal2-metal6 metal4 demo_top 0\n"
        in done.stdout
    )
    assert "RB-WIRE-RC: 0\n" in done.stdout
    assert done.stdout.endswith(f"AFTER {out_dir.resolve()}\n")


def test_a_run_without_session_hooks_has_none(tmp_path, monkeypatch):
    backend, artefacts = _run_backend(
        tmp_path, monkeypatch, {"after-cts": ("/h.tcl",)}, checkpoints=None
    )

    assert _prelude(backend, artefacts, tmp_path / "power") == ""


@pytest.mark.parametrize(
    ("caps", "printed"),
    [
        ({"a": (1e-3, 2e-3), "b": (1e-3, 1e-3)}, "1"),
        ({"a": (1e-3, 2e-3), "b": (0.0, 1e-3)}, "0"),
        ({"a": (1e-3, 0.0)}, "0"),
        ({}, "0"),
        (None, "unknown"),
    ],
)
def test_the_wire_rc_probe_needs_signal_and_clock_capacitance_in_every_scene(
    tmp_path, caps, printed
):
    from rtl_buddy.tools.pnr_openroad import _wire_rc_probe_tcl

    stubs = ""
    if caps is not None:
        signal = " ".join(f"{scene} {c[0]}" for scene, c in caps.items())
        clock = " ".join(f"{scene} {c[1]}" for scene, c in caps.items())
        stubs = (
            f"namespace eval sta {{ proc scenes {{}} {{ return {{{' '.join(caps)}}} }} }}\n"
            "namespace eval est {\n"
            f"  proc wire_signal_capacitance {{s}} {{ return [dict get {{{signal}}} $s] }}\n"
            f"  proc wire_clk_capacitance {{s}} {{ return [dict get {{{clock}}} $s] }}\n"
            "}\n"
        )

    done = _tcl_run(tmp_path, stubs + _wire_rc_probe_tcl())

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"RB-WIRE-RC: {printed}\n"
