"""Tests for ``rb release``: config schema, lexical helpers, name map, SDC rewrite and the flow.

The flow tests run against ``tests/fixtures/release_project`` with stand-ins for
Verible, VCS and the simulator from ``tests/release_fakes``. Every gate the flow
relies on is shown to fail on input built to trip it. A last test runs the real
tools when they and an IEEE-1735 key file (``RB_RELEASE_TEST_KEY``) are present.
"""

from __future__ import annotations

import importlib.util
import json
import re
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.release import sdc
from rtl_buddy.release.config import load_release_config
from rtl_buddy.release.encrypt import plaintext_outside_envelope
from rtl_buddy.release.flow import ReleaseFlow, ReleaseOptions
from rtl_buddy.release.namemap import NameMap, previous_map, seed_map, version_key
from rtl_buddy.release.obfuscate import leaked_names
from rtl_buddy.release.sv_text import (
    declared_units,
    header_names,
    identifiers,
    include_targets,
    lexical_hazards,
    strip_comments,
)
from rtl_buddy.rtl_buddy import RtlBuddy

FIXTURE = Path(__file__).parent / "fixtures" / "release_project"
FAKES = Path(__file__).parent / "release_fakes"
REL = "release/acme_cut"


# ---- helpers -----------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture project wired to the fake tools; returns the project root."""
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    rel = root / REL
    (rel / "keys.txt").write_text("fake key\n")
    _edit(
        rel / "release.yaml",
        lambda d: (
            d.update(
                {
                    "verify": {
                        "command": f"python3 {FAKES / 'sim.py'} design/acme_top.f verif/tb.f",
                        "pass": "^TEST PASSED",
                        "compare": "^SIG: .*",
                    },
                    "obfuscation": {
                        "verible": str(FAKES / "verible-verilog-obfuscate")
                    },
                }
            ),
            d["encryption"].update({"vcs": str(FAKES / "vcs")}),
        ),
    )
    monkeypatch.chdir(rel)
    return root


def _edit(path: Path, fn) -> None:
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _run(root: Path, **opts) -> Path:
    cfg = load_release_config(root / REL / "release.yaml")
    out = root / REL / "artefacts" / f"{cfg.name}-{cfg.version}"
    return ReleaseFlow(cfg, out, ReleaseOptions(**opts)).run()


def _release_files(tarball: Path) -> dict[str, str]:
    with tarfile.open(tarball) as tar:
        return {
            m.name.split("/", 1)[1]: tar.extractfile(m).read().decode()
            for m in tar.getmembers()
            if m.isfile()
        }


# ---- config ------------------------------------------------------------------


def _write_cfg(tmp_path: Path, extra: dict) -> Path:
    base = {
        "rtl-buddy-filetype": "release_config",
        "name": "x",
        "version": "1.0",
        "design": {"model": "m", "model-config": "models.yaml"},
        "encryption": {"key-file": "k.txt"},
    }
    base.update(extra)
    p = tmp_path / "release.yaml"
    p.write_text(yaml.safe_dump(base))
    return p


def test_config_defaults_protect_the_design_and_not_the_testbench(tmp_path: Path):
    cfg = load_release_config(_write_cfg(tmp_path, {"testbench": {"filelist": []}}))
    assert cfg.design.protect.obfuscate and cfg.design.protect.encrypt
    assert cfg.design.protect.strip_comments
    assert cfg.testbench is not None
    assert not cfg.testbench.protect.obfuscate and not cfg.testbench.protect.encrypt
    assert cfg.map_path() == tmp_path / "maps" / "1.0.map"


def test_config_rejects_an_unknown_key(tmp_path: Path):
    p = _write_cfg(tmp_path, {})
    _edit(p, lambda d: d["design"].update({"protetc": {"encrypt": False}}))
    with pytest.raises(FatalRtlBuddyError, match="unknown key.*protetc"):
        load_release_config(p)


def test_config_rejects_a_file_rule_without_a_reason(tmp_path: Path):
    p = _write_cfg(tmp_path, {})
    _edit(
        p,
        lambda d: d["design"].update({"files": [{"match": "a.sv", "encrypt": False}]}),
    )
    with pytest.raises(FatalRtlBuddyError, match="reason"):
        load_release_config(p)


def test_config_rejects_testbench_obfuscation(tmp_path: Path):
    p = _write_cfg(tmp_path, {"testbench": {"protect": {"obfuscate": True}}})
    with pytest.raises(FatalRtlBuddyError, match="obfuscation is not supported"):
        load_release_config(p)


def test_config_rejects_a_version_that_cannot_name_a_file(tmp_path: Path):
    p = _write_cfg(tmp_path, {"version": "1.0/beta"})
    with pytest.raises(FatalRtlBuddyError, match="version"):
        load_release_config(p)


# ---- lexical helpers ---------------------------------------------------------


def test_strip_comments_keeps_directives_and_line_numbers():
    text = 'a = 1; // secret\n/* two\nlines */ b = "// not a comment";\n// synopsys translate_off\n'
    out = strip_comments(text, __import__("re").compile("synopsys"))
    assert "secret" not in out and "two" not in out
    assert '"// not a comment"' in out
    assert "// synopsys translate_off" in out
    assert out.count("\n") == text.count("\n")


def test_identifiers_skip_comments_strings_and_system_tasks():
    text = 'logic foo; // bar\ninitial $display("baz %d", foo); x = 8\'hFF;'
    assert identifiers(text) == {"logic", "foo", "initial", "x"}


def test_lexical_hazards_flag_token_pasting_and_macro_strings():
    text = '`define NXT(a) a``_nxt\n`define S(a) `"a`"\nlogic x;\n'
    rules = [(h.line, h.rule) for h in lexical_hazards(text)]
    assert rules == [(1, "token-paste"), (2, "macro-string")]
    assert lexical_hazards("logic x; // a``b\n") == []
    # Delimiting an argument does not form a new name.
    assert lexical_hazards("`define AS(I,O) assign ``O``.pid = ``I``.pid;\n") == []


def test_declared_units_and_includes():
    text = 'package p; endpackage\nmodule automatic m; `include "h.svh" endmodule\ninterface class c; endclass'
    assert declared_units(text) == {"p", "m", "c"}
    assert include_targets(text) == ["h.svh"]


# ---- name map ----------------------------------------------------------------


def test_seed_map_carries_names_over_and_drops_collisions():
    prev = NameMap({"cnt": "Xq1", "top": "Ab3", "sig": "keep"})
    seed, dropped = seed_map({"top", "keep"}, prev)
    assert seed.entries["cnt"] == "Xq1"
    assert seed.entries["top"] == "top"
    assert "sig" not in seed.entries
    assert dropped == ["sig", "top"]


def test_previous_map_orders_versions_numerically(tmp_path: Path):
    for v in ("1.9.0", "1.10.0", "2.0.0"):
        (tmp_path / f"{v}.map").write_text("")
    assert previous_map(tmp_path, "2.0.0").name == "1.10.0.map"
    assert previous_map(tmp_path, "1.0.0") is None
    assert version_key("1.10") > version_key("1.9")


def test_name_map_rejects_two_names_for_one_spelling():
    with pytest.raises(FatalRtlBuddyError, match="not one-to-one"):
        NameMap({"a": "X", "b": "X"}).check_injective()


def test_leak_check_finds_an_original_name():
    nm = NameMap({"secret": "Qw3Er5", "kept": "kept"})
    assert leaked_names("logic Qw3Er5, kept;", nm) == set()
    assert leaked_names("logic secret;", nm) == {"secret"}


def test_envelope_check_flags_plaintext():
    clean = "`pragma protect begin_protected\nAbCd==\n`pragma protect end_protected\n"
    assert plaintext_outside_envelope(clean) == []
    assert plaintext_outside_envelope("module leak;\n" + clean) == ["module leak;"]
    assert plaintext_outside_envelope("`pragma protect begin_protected\nAb\n")


# ---- SDC -----------------------------------------------------------------------


def test_sdc_rewrite_expands_loops_and_translates_paths(tmp_path: Path, tcl_backend):
    f = tmp_path / "c.sdc"
    f.write_text(
        "foreach p {a_i b_i} { set_input_delay 1 -clock c [get_ports $p] }\n"
        "set_false_path -to [get_pins -of_objects [get_cells {u_x}]/D]\n"
        "set_multicycle_path 2 -through [get_pins {u_x/cnt_reg[3]/Q}]\n"
        "set_clock_groups -asynchronous -group {c} -group [get_clocks d]\n"
    )
    text, errors = sdc.rewrite(
        f, NameMap({"u_x": "Kp9", "cnt": "Zz1"}), "top", {"a_i", "b_i"}
    )
    assert errors == []
    assert "[get_ports {a_i}]" in text and "[get_ports {b_i}]" in text
    assert '"[get_cells {Kp9}]/D"' in text
    assert "[get_pins {Kp9/Zz1_reg[3]/Q}]" in text
    assert "-group c -group [get_clocks {d}]" in text


def test_sdc_rewrite_rejects_stale_ports_and_wildcards_over_renamed_names(
    tmp_path: Path, tcl_backend
):
    f = tmp_path / "c.sdc"
    f.write_text(
        "set_input_delay 1 -clock c [get_ports gone_i]\n"
        "set_false_path -to [get_cells u_*]\n"
        "set_false_path -to [get_cells -filter {name=~u*}]\n"
        "source other.sdc\n"
    )
    _, errors = sdc.rewrite(f, NameMap({"u_x": "Kp9"}), "top", {"a_i"})
    assert any("gone_i" in e for e in errors)
    assert any("u_*" in e for e in errors)
    assert any("-filter" in e for e in errors)
    assert any("source" in e for e in errors)


def test_verbatim_constraints_are_checked_not_rewritten(tmp_path: Path):
    f = tmp_path / "c.sdc"
    f.write_text(
        "# get_cells in a comment is fine\n"
        "foreach p [get_ports {a_* b_i}] { set_input_delay 1 $p }\n"
        "set x [get_ports $name]\n"
    )
    assert sdc.check_verbatim(f, "top", {"a_0", "b_i"}) == []
    f.write_text(
        "set_false_path -to [get_pins u_x/D]\nset_input_delay 1 [get_ports gone]\n"
    )
    problems = sdc.check_verbatim(f, "top", {"a_0"})
    assert len(problems) == 2
    assert "internal objects" in problems[0] and "gone" in problems[1]


# ---- flow ----------------------------------------------------------------------


def test_release_packages_protected_design_and_clear_testbench(
    project: Path, tcl_backend
):
    tarball = _run(project)
    files = _release_files(tarball)
    assert {
        "design/acme_top.svp",
        "design/acme_core.svp",
        "design/acme_pkg.svp",
        "design/acme_defs.svh",
        "design/acme_top.f",
        "design/constraints/acme_top.sdc",
        "verif/tb_acme.sv",
        "verif/tb.f",
        "verif/run.sh",
        "docs/user_guide.md",
        "RELEASE_NOTES.md",
        "MANIFEST",
    } <= set(files)
    for name, text in files.items():
        if name.endswith((".svp", ".svh")):
            assert plaintext_outside_envelope(text) == [], name
    assert "+define+ACME_FAST" in files["design/acme_top.f"]
    assert "design/acme_core.svp" in files["design/acme_top.f"]
    # Directive comments survive; the testbench ships as written.
    assert files["verif/tb_acme.sv"] == (project / REL / "tb/tb_acme.sv").read_text()
    sdc_text = files["design/constraints/acme_top.sdc"]
    assert "[get_ports {en_i}]" in sdc_text and "[get_ports {step_i*}]" in sdc_text
    # The map and manifest are archived for a clean, verified release.
    nm = NameMap.load(project / REL / "maps" / "1.0.0.map")
    assert nm.entries["acme_top"] == "acme_top"
    assert nm.entries["clk_i"] == "clk_i"
    assert nm.entries["ACME_FAST"] == "ACME_FAST"
    assert nm.entries["acme_core"] != "acme_core"
    # Built-in method names are kept everywhere, so `lim.min` matches its field.
    assert nm.entries["min"] == "min"
    assert nm.entries["__FILE__"] == "__FILE__"
    assert nm.entries["push_back"] == "push_back"
    assert nm.entries["seen_q"] != "seen_q"
    obf_core = (
        project / REL / "artefacts/acme-1.0.0/stage/obf/design/acme_core.sv"
    ).read_text()
    assert "acme_counter" not in obf_core and "Core:" not in obf_core


def test_next_release_keeps_the_previous_names(project: Path, tcl_backend):
    _run(project)
    rel = project / REL
    _edit(rel / "release.yaml", lambda d: d.update({"version": "1.1.0"}))
    (rel / "notes/1.1.0.md").write_text("# 1.1.0\n")
    _run(project)
    first = NameMap.load(rel / "maps/1.0.0.map").renamed()
    second = NameMap.load(rel / "maps/1.1.0.map").renamed()
    assert first == second


def test_a_released_version_is_not_overwritten(project: Path, tcl_backend):
    _run(project)
    with pytest.raises(FatalRtlBuddyError, match="already exists"):
        _run(project)


def test_trial_release_archives_nothing_and_can_repeat(project: Path, tcl_backend):
    _run(project, trial=True)
    _run(project, trial=True)
    assert not (project / REL / "maps").exists()


def test_verification_failure_in_one_stage_fails_the_release(
    project: Path, tcl_backend, monkeypatch
):
    monkeypatch.setenv("RB_FAKE_SIM_SIG_pkg", "different")
    with pytest.raises(
        FatalRtlBuddyError, match=r"stage\(s\) \['pkg'\] produced different"
    ):
        _run(project)
    assert not (project / REL / "maps/1.0.0.map").exists()


def test_failing_testbench_fails_the_release(project: Path, tcl_backend):
    _edit(
        project / REL / "release.yaml",
        lambda d: d["verify"].update({"pass": "^NEVER PRINTED"}),
    )
    with pytest.raises(FatalRtlBuddyError, match="verification failed"):
        _run(project)


def test_testbench_naming_a_private_unit_is_refused(project: Path, tcl_backend):
    tb = project / REL / "tb/tb_acme.sv"
    tb.write_text(
        tb.read_text().replace("acme_top dut", "acme_top dut ();\n  acme_core peek")
    )
    with pytest.raises(FatalRtlBuddyError, match="acme_core"):
        _run(project)
    _edit(
        project / REL / "release.yaml",
        lambda d: d["testbench"].update({"allow-design-refs": ["acme_core"]}),
    )
    # Allowed: acme_core is published by name; the design still obfuscates the rest.
    _run(project, verify=False)


def test_token_pasting_is_refused_or_preserved(project: Path, tcl_backend):
    core = project / "rtl/acme_core.sv"
    core.write_text(
        core.read_text().replace(
            "  logic [11:0] acc_a, acc_b;",
            "  logic [11:0] acc_a, acc_b;\n"
            "  `define NXT(s) s``_nxt\n"
            "  logic [11:0] acc_a_nxt;\n"
            "  assign `NXT(acc_a) = acc_a;",
        )
    )
    with pytest.raises(FatalRtlBuddyError, match="token-paste"):
        _run(project)
    _edit(
        project / REL / "release.yaml",
        lambda d: d["obfuscation"].update({"token-paste": "preserve"}),
    )
    _run(project)
    nm = NameMap.load(project / REL / "maps/1.0.0.map")
    # The pasted name, its literal piece and the identifier pasted into it keep their spelling.
    assert nm.entries["acc_a_nxt"] == "acc_a_nxt"
    assert nm.entries["_nxt"] == "_nxt"
    assert nm.entries["acc_a"] == "acc_a"
    assert nm.entries["acc_b"] != "acc_b"


def test_stale_file_rule_is_refused(project: Path, tcl_backend):
    _edit(
        project / REL / "release.yaml",
        lambda d: d["design"].update(
            {"files": [{"match": "gone.sv", "encrypt": False, "reason": "was public"}]}
        ),
    )
    with pytest.raises(FatalRtlBuddyError, match="gone.sv"):
        _run(project)


def test_file_rule_ships_an_unencrypted_file(project: Path, tcl_backend):
    _edit(
        project / REL / "release.yaml",
        lambda d: d["design"].update(
            {
                "files": [
                    {
                        "match": "acme_pkg.sv",
                        "encrypt": False,
                        "reason": "types the customer needs",
                    }
                ]
            }
        ),
    )
    files = _release_files(_run(project))
    assert "design/acme_pkg.sv" in files and "design/acme_pkg.svp" not in files
    # Not encrypted, still obfuscated: its private names are gone.
    assert "STEP_SCALE" not in files["design/acme_pkg.sv"]


def test_stale_constraint_port_is_refused(project: Path, tcl_backend):
    sdc_file = project / "rtl/acme_top.sdc"
    sdc_file.write_text(
        sdc_file.read_text() + "set_false_path -from [get_ports old_i]\n"
    )
    with pytest.raises(FatalRtlBuddyError, match="old_i"):
        _run(project)


def test_missing_release_notes_are_refused(project: Path):
    (project / REL / "notes/1.0.0.md").unlink()
    with pytest.raises(FatalRtlBuddyError, match="release notes"):
        _run(project)


def test_dirty_tree_is_refused_and_a_trial_is_not_archived(project: Path, tcl_backend):
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q"], cwd=project, check=True)
    subprocess.run(git + ["add", "-A"], cwd=project, check=True)
    subprocess.run(git + ["commit", "-qm", "init"], cwd=project, check=True)
    core = project / "rtl/acme_core.sv"
    core.write_text(core.read_text() + "\n")
    with pytest.raises(FatalRtlBuddyError, match="uncommitted"):
        _run(project)
    _run(project, allow_dirty=True)
    assert not (project / REL / "maps/1.0.0.map").exists()


def test_cli_runs_the_release(project: Path, tcl_backend):
    result = CliRunner().invoke(RtlBuddy(name="test_release").app, ["release"])
    assert result.exit_code == 0, result.output
    assert (project / REL / "artefacts/acme-1.0.0/acme-1.0.0.tar.gz").is_file()


# ---- real tools ------------------------------------------------------------------


@pytest.mark.skipif(
    not (
        shutil.which("verible-verilog-obfuscate")
        and shutil.which("vcs")
        and os.environ.get("RB_RELEASE_TEST_KEY")
    ),
    reason="needs verible-verilog-obfuscate and vcs on PATH and RB_RELEASE_TEST_KEY",
)
def test_real_tools_release_simulates_like_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tcl_backend
):
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    shutil.copy(os.environ["RB_RELEASE_TEST_KEY"], root / REL / "keys.txt")
    monkeypatch.chdir(root / REL)
    files = _release_files(_run(root))
    assert "design/acme_core.svp" in files


# ---- reproducibility ---------------------------------------------------------------


def _git_commit_all(root: Path, message: str = "release") -> None:
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    if not (root / ".git").exists():
        subprocess.run(git + ["init", "-q"], cwd=root, check=True)
        (root / ".gitignore").write_text("artefacts/\nrtl_buddy.log\n")
    subprocess.run(git + ["add", "-A"], cwd=root, check=True)
    subprocess.run(git + ["commit", "-qm", message], cwd=root, check=True)


def test_untracked_header_on_the_include_path_is_refused(project: Path, tcl_backend):
    _git_commit_all(project)
    # Beside the including file, so it wins over the committed rtl/inc copy.
    shadow = project / "rtl/acme_defs.svh"
    shadow.write_text((project / "rtl/inc/acme_defs.svh").read_text() + "\n")
    with pytest.raises(FatalRtlBuddyError, match="uncommitted"):
        _run(project)


def test_gitignored_input_is_refused(project: Path, tcl_backend):
    _git_commit_all(project)
    (project / ".gitignore").write_text(
        "artefacts/\nrtl_buddy.log\nrtl/acme_defs.svh\n"
    )
    _git_commit_all(project, "ignore a header")
    (project / "rtl/acme_defs.svh").write_text(
        (project / "rtl/inc/acme_defs.svh").read_text()
    )
    with pytest.raises(FatalRtlBuddyError, match=r"acme_defs.svh \(gitignored\)"):
        _run(project)


def test_the_tarball_is_byte_identical_across_runs(project: Path, tcl_backend):
    _git_commit_all(project)
    first = _run(project, trial=True).read_bytes()
    second = _run(project, trial=True).read_bytes()
    assert first == second


def test_reproduce_recuts_a_release_from_its_map(
    project: Path, tcl_backend, monkeypatch, tmp_path: Path
):
    rel = project / REL
    _git_commit_all(project)
    released_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setenv("RB_FAKE_VERIBLE_SALT", "first")
    _run(project)
    released = NameMap.load(rel / "maps/1.0.0.map")
    ref = tmp_path / "ref"
    ref.mkdir()
    shutil.copy(rel / "maps/1.0.0.json", ref / "1.0.0.json")
    shutil.copy(rel / "maps/1.0.0.map", ref / "1.0.0.map")
    # Back to the released commit, without the archived map: a fresh cut there
    # picks different names, as the real obfuscator does...
    shutil.rmtree(rel / "maps")
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True
        ).stdout.strip()
        == released_commit
    )
    monkeypatch.setenv("RB_FAKE_VERIBLE_SALT", "second")
    _run(project, trial=True)
    fresh = NameMap.load(rel / "artefacts/acme-1.0.0/1.0.0.map")
    assert fresh.renamed() != released.renamed()
    # ...but a reproduction pins them and matches every shipped file's plaintext.
    _run(project, reproduce=ref / "1.0.0.json")
    # A changed released digest is caught.
    doc = json.loads((ref / "1.0.0.json").read_text())
    doc["files"][0]["plaintext_sha256"] = "0" * 64
    (ref / "1.0.0.json").write_text(json.dumps(doc))
    with pytest.raises(FatalRtlBuddyError, match="plaintext differs"):
        _run(project, reproduce=ref / "1.0.0.json")
    # So is a cut from another commit.
    (project / "rtl/acme_top.sv").write_text(
        (project / "rtl/acme_top.sv").read_text() + "\n"
    )
    _git_commit_all(project, "later change")
    with pytest.raises(FatalRtlBuddyError, match="check out the release tag"):
        _run(project, reproduce=ref / "1.0.0.json")


def test_a_rule_moves_a_file_to_its_own_package_directory(project: Path, tcl_backend):
    _edit(
        project / REL / "release.yaml",
        lambda d: d["design"].update(
            {
                "files": [
                    {
                        "match": "rtl/acme_counter.sv",
                        "dir": "design_cust_to_replace",
                        "obfuscate": False,
                        "encrypt": False,
                        "reason": "behavioural model the customer replaces",
                    }
                ]
            }
        ),
    )
    files = _release_files(_run(project))
    assert "design_cust_to_replace/acme_counter.sv" in files
    assert "design/acme_counter.sv" not in files
    assert "design_cust_to_replace/acme_counter.sv" not in files["design/acme_top.f"]
    assert files["design_cust_to_replace/design_cust_to_replace.f"].splitlines()[
        2:
    ] == [
        "+incdir+design_cust_to_replace",
        "design_cust_to_replace/acme_counter.sv",
    ]
    order = [
        ln for ln in files["sim.f"].splitlines() if ln.startswith(("design", "verif"))
    ]
    assert order.index("design_cust_to_replace/acme_counter.sv") < order.index(
        "design/acme_core.svp"
    )
    assert "+incdir+design_cust_to_replace" in files["sim.f"]


def test_a_rule_directory_must_stay_inside_the_package(tmp_path: Path):
    p = _write_cfg(tmp_path, {})
    _edit(
        p,
        lambda d: d["design"].update(
            {"files": [{"match": "a.sv", "dir": "../out", "reason": "x"}]}
        ),
    )
    with pytest.raises(FatalRtlBuddyError, match="relative package directory"):
        load_release_config(p)


def test_header_names_cover_ansi_and_non_ansi_headers():
    text = (
        "module m1 #(parameter W = 2) (input [W-1:0] a, output b);\n"
        "  wire hidden;\n"
        "endmodule\n"
        "module m2 (c, d);\n"
        "  parameter DEPTH = 4;\n"
        "  input c; output d; reg secret;\n"
        "endmodule\n"
    )
    names = header_names(text)
    assert {"m1", "W", "a", "b", "m2", "c", "d", "DEPTH"} <= names
    assert not {"hidden", "secret"} & names


def test_an_external_goes_to_its_directory_once(project: Path, tcl_backend):
    vendor = project / "vendor_mem"
    vendor.mkdir()
    (vendor / "vmem.v").write_text("module vmem (input clk); endmodule\n")
    acme_f = project / "rtl/acme.f"
    acme_f.write_text(
        acme_f.read_text() + "-v ../vendor_mem/vmem.v\n-v ../vendor_mem/vmem.v\n"
    )
    _edit(
        project / REL / "release.yaml",
        lambda d: d["design"].update(
            {
                "externals": [
                    {
                        "path": "../../vendor_mem",
                        "ship-as": "$VMEM_DIR",
                        "dir": "design_cust_to_replace",
                    }
                ]
            }
        ),
    )
    files = _release_files(_run(project, verify=False))
    assert "-v $VMEM_DIR/vmem.v" not in files["design/acme_top.f"]
    assert (
        files["design_cust_to_replace/design_cust_to_replace.f"].count(
            "-v $VMEM_DIR/vmem.v"
        )
        == 1
    )
    assert files["sim.f"].count("-v $VMEM_DIR/vmem.v") == 1
    assert not any(n.endswith("vmem.v") for n in files)


# ---- csr ---------------------------------------------------------------------

csr_tools = pytest.mark.skipif(
    any(
        importlib.util.find_spec(m) is None
        for m in ("systemrdl", "peakrdl_systemrdl", "peakrdl_cheader")
    ),
    reason="needs the release-csr extra",
)


def _add_csr(root: Path) -> None:
    _edit(
        root / REL / "release.yaml",
        lambda d: d.update(
            {
                "csr": {
                    "gate": "acme_customer",
                    "windows": [
                        {
                            "name": "main",
                            "rdl": "../../rtl/csr/acme_csr.rdl",
                            "top": "acme_csr",
                            "base": 0x4000_0000,
                        }
                    ],
                    "registers": [
                        {"match": "main.ctrl", "obfuscate-fields": ["dbg_*"]},
                        {"match": "main.chan.*"},
                    ],
                }
            }
        ),
    )


@csr_tools
def test_release_ships_the_register_map_and_records_it(project: Path, tcl_backend):
    _add_csr(project)
    files = _release_files(_run(project))
    assert {f for f in files if f.startswith("csr/")} == {
        "csr/acme_csr.rdl",
        "csr/acme_csr.h",
        "csr/acme_csr.svh",
        "csr/acme_csr.md",
    }
    assert re.search(r"\sclear\s+csr/acme_csr.svh$", files["MANIFEST"], re.M)
    manifest = json.loads((project / REL / "maps/1.0.0.json").read_text())
    assert manifest["csr"]["registers"] == {"main": 5}
    assert set(manifest["csr"]["files"]) == {
        "csr/acme_csr.rdl",
        "csr/acme_csr.h",
        "csr/acme_csr.svh",
        "csr/acme_csr.md",
    }


@csr_tools
def test_csr_only_writes_just_the_map(project: Path):
    _add_csr(project)
    out = _run(project, csr_only=True)
    assert sorted(p.name for p in out.iterdir()) == [
        "acme_csr.h",
        "acme_csr.md",
        "acme_csr.rdl",
        "acme_csr.svh",
    ]
    assert not (out.parent / "stage").exists()


@csr_tools
def test_an_ignored_rdl_include_is_refused(project: Path, tcl_backend):
    _add_csr(project)
    _git_commit_all(project)
    (project / ".gitignore").write_text(
        "artefacts/\nrtl_buddy.log\nrtl/csr/acme_udp.rdl\n"
    )
    subprocess.run(
        ["git", "rm", "-q", "--cached", "rtl/csr/acme_udp.rdl"], cwd=project, check=True
    )
    _git_commit_all(project, "stop tracking the properties")
    with pytest.raises(FatalRtlBuddyError, match=r"acme_udp.rdl \(gitignored\)"):
        _run(project)
