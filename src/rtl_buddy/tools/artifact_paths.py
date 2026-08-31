from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable
import re

from ..errors import FatalRtlBuddyError

#: The suite-relative directory every rtl_buddy artefact tree is written
#: into. Named here rather than spelled out at each use so consumers that
#: must *recognise* rtl_buddy's own output — the shared-build stamp's
#: directory listings, for one (#478) — derive it from the same place the
#: writers do.
ARTIFACT_DIRNAME = "artefacts"

#: Holds the compile-key-named shared build directories, under
#: :data:`ARTIFACT_DIRNAME`.
SHARED_BUILDS_DIRNAME = ".shared-builds"

#: Every simulator build directory rtl_buddy names starts with this, both
#: the per-test ``obj_dir_<test>`` and the shared ``obj_dir_<key>``.
BUILD_DIR_PREFIX = "obj_dir"

#: The per-run result envelope the in-process runner writes into a test's
#: artefact directory. Named here for the same reason as the directories
#: above: it is an rtl_buddy *output*, and the shared-build stamp has to be
#: able to tell one of those from a generated input (#478).
RESULT_JSON_NAME = "result.json"

#: A dispatched job's envelope and log, written into
#: ``<test>/dispatch/`` (per test) and ``artefacts/.dispatch/`` (per head)
#: — see :mod:`rtl_buddy.dispatch.argv`. fnmatch patterns, because both
#: carry a per-job tag.
DISPATCH_OUTPUT_PATTERNS = (
    "result-*.json",
    "build-result-*.json",
    "rtl_buddy-*.log",
    "build-rtl_buddy-*.log",
)

#: rtl_buddy's *own* bookkeeping, as fnmatch patterns. These share an
#: artefact directory with the tool outputs but are not tool outputs, and
#: nothing that clears by suffix may remove them. It matters because an
#: artefact directory is keyed on a run's *name* and names are not required
#: to be unique across commands: an `rb fpga` run and a simulation test
#: called the same thing land in the same `artefacts/<name>/`, where the
#: FPGA backend's `.json` suffix would otherwise match the test's durable
#: `result.json` (#469). Enforced inside :func:`clear_managed_outputs`
#: rather than left to each caller's ``keep``, because a caller that
#: forgets is precisely the bug.
#: The fixed-name durable outputs the *other* commands write into
#: ``artefacts/<name>/`` and read back later. An artefact directory is keyed
#: on a run's name and names need not be unique across commands, so a CDC
#: analysis and an FPGA run called the same thing share one directory — where
#: the FPGA backend's ``.json`` suffix clear would otherwise eat ``cdc.json``
#: and the domain maps (#469). Listed here, not at each call site, because a
#: suffix clear cannot tell whose file it is looking at.
#:
#: Only *fixed* names belong here. Every flow clears its own fixed-name
#: outputs through :func:`clear_stale_artefacts`, which does not consult this
#: set, so protecting them costs an owner nothing; the outputs a flow does
#: clear by suffix are all named after its design's top and so cannot be
#: spelled as constants anyway. ``tests/test_vlog_sim_paths.py`` pins both
#: halves of that: every name below is one a flow really writes, and none of
#: them is one a flow clears by suffix.
SIBLING_OUTPUT_NAMES = (
    # rb cdc, open analyzer (tools/cdc_rtl_buddy.py)
    "cdc.json",
    "cdc.txt",
    "domain_map.json",
    "reset_map.json",
    # rb cdc, Vivado backend (tools/cdc_vivado.py)
    "cdc.rpt",
    # rb power (tools/power_openroad.py)
    "power.rpt",
    # rb pnr's design-independent reports (tools/pnr_openroad.py)
    "route.drc.rpt",
    "timing.rpt",
    # rb synth (tools/synth_yosys.py, tools/synth_openroad.py)
    "synth_netlist.v",
    "synth.rtlil",
    # rb axi-profile (tools/axi_profile_rtl_buddy.py). Lives in its own
    # `artefacts/axi/<name>/` subtree today, so nothing can reach it — listed
    # so that stays true if a flow ever globs `.json` there.
    "axi-perf.json",
)

#: Everything :func:`clear_managed_outputs` must never remove: rtl_buddy's own
#: bookkeeping plus the sibling commands' durable outputs above.
PROTECTED_OUTPUT_PATTERNS = (
    RESULT_JSON_NAME,
    *DISPATCH_OUTPUT_PATTERNS,
    *SIBLING_OUTPUT_NAMES,
)


def sanitize_artifact_component(name: str) -> str:
    """
    Return a filesystem-safe artifact path component.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def test_artifact_dir(
    suite_dir: str | Path, test_name: str, run_id: int | None = None
) -> Path:
    """
    Return the per-test artifact directory rooted under the suite directory.
    """
    artifact_dir = (
        Path(suite_dir) / ARTIFACT_DIRNAME / sanitize_artifact_component(test_name)
    )
    if run_id is not None:
        artifact_dir /= f"run-{run_id:04d}"
    return artifact_dir


def test_build_dir_name(test_name: str) -> str:
    """
    Return the simulator build directory name for a test.
    """
    return f"{BUILD_DIR_PREFIX}_{sanitize_artifact_component(test_name)}"


def shared_build_dir(suite_dir: str | Path, compile_key: str) -> Path:
    """
    Return the compile-input-keyed build directory shared by all tests in a
    suite whose compile inputs hash to ``compile_key``.

    Lives under a dot-directory so it can never collide with a per-test
    artifact directory derived from a test name.
    """
    return (
        Path(suite_dir)
        / ARTIFACT_DIRNAME
        / SHARED_BUILDS_DIRNAME
        / f"{BUILD_DIR_PREFIX}_{compile_key}"
    )


def clear_stale_artefacts(
    paths: Iterable[str | Path | None], *, owner: str
) -> list[str]:
    """Delete a tool's outputs *before* invoking the tool that writes them.

    Every tool flow in rtl_buddy runs a subprocess and then reads its
    outputs back off fixed paths in the run's artefact directory. An exit
    code cannot separate "ran clean and produced nothing to report" from
    "crashed before writing" — rtl-buddy-cdc's exit 1 means "rule
    violations found", so a crash that happens to exit 1 walks straight
    past the returncode gate. When an earlier run's report is still lying
    in the artefact dir, that stale file is parsed and its numbers are
    reported as the current result (#469).

    Clearing the outputs first makes presence proof of authorship: what
    exists afterwards was written by this invocation, and what is absent
    takes the flow's existing "not produced" path — which is the honest
    answer and already points the user at the log.

    Logs are deliberately *not* passed here: each flow either truncates
    its own log (``open(path, "w")``) or hands the path to a tool that
    does, and the log is the one artefact worth keeping if the tool dies
    before it can write anything else.

    **Call this early.** Clearing just before the subprocess is not enough:
    a rerun that fails on any path *before* the tool — a filelist error, an
    unresolvable config, a gate that returns early — leaves the previous
    run's outputs exactly where the next reader looks. Two placements are
    correct, and which one applies depends on who reads the artefact:

    - Outputs a **later command** consumes (the synthesis netlists that
      ``rb pnr`` / ``rb power`` resolve; pnr's DEF and ODB) must be cleared
      as the *first* action of ``run()``, ahead of every validation and
      tool-availability check. A missing tool still means no fresh netlist,
      and the downstream command must not silently use the old one.
    - Outputs only read back within the same ``run()`` (the CDC, FPGA and
      power reports, the bitstream) are cleared immediately *after* the
      tool-availability skip and before all other work. A box that lacks
      the tool provably never ran it, so it has no business deleting what a
      box that has the tool produced; every other exit path clears.

    Args:
      paths: the outputs this invocation is expected to (re)write.
        Entries that do not exist are ignored, and ``None`` entries are
        skipped so callers can splice in conditional artefacts.
      owner: the run/analysis name, used in the error message.

    Returns:
      The paths that actually existed, in the order given — for logging.

    Raises:
      FatalRtlBuddyError: an existing artefact could not be removed.
        Running on would risk reporting a previous run's numbers, so this
        fails loudly instead.
    """
    removed: list[str] = []
    for entry in paths:
        if entry is None:
            continue
        path = Path(entry)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            raise FatalRtlBuddyError(
                f"{owner}: could not remove the previous run's artefact {path}: {e}"
            ) from e
        removed.append(str(path))
    return removed


def clear_managed_outputs(
    artefact_dir: str | Path,
    suffixes: Iterable[str],
    *,
    owner: str,
    keep: Iterable[str] = (),
) -> list[str]:
    """Clear a run's outputs by *suffix* rather than by exact name.

    :func:`clear_stale_artefacts` can only remove paths it can name, and
    several flows name their outputs after the design's top module —
    ``<top>.bit``, ``<design>.routed.odb``. Editing a run's ``model:`` or
    ``top:`` therefore renamed the outputs and left the previous top's
    files behind in the same directory, still at the fixed paths a later
    command or a later edit-back would resolve (#469).

    Matching on the suffix instead makes the clear independent of the top,
    which is safe here because an artefact directory belongs to exactly one
    run: everything in it was put there by that run, so anything carrying a
    suffix this flow manages is by definition this flow's own output. Only
    the directory itself is scanned — never recursively, so a nested
    workdir a tool owns is untouched.

    Args:
      artefact_dir: the run's artefact directory. A missing directory is
        not an error; there is simply nothing to clear.
      suffixes: the filename suffixes this flow writes (``".bit"``,
        ``".routed.odb"``). Include the dot. Match a *log* suffix here and
        you defeat the log exemption, so don't.
      owner: the run/analysis name, for the error message.
      keep: exact filenames to leave alone even when they match — for a
        fixed-name artefact that happens to share a managed suffix.
        rtl_buddy's own envelopes (:data:`PROTECTED_OUTPUT_PATTERNS`) are
        always kept and need not be listed here.

    Every matching entry is handed to :func:`clear_stale_artefacts`,
    including ones that are not regular files. A *directory* sitting where
    an output belongs (``<top>.bit/``) is exactly the case that must not be
    skipped: something has to be removed before the tool can write there,
    and quietly ignoring it would let the run read a neighbouring stale file
    or report success against a path it never wrote. Unlinking a directory
    fails, so it takes the documented fatal path and the user is told which
    path to deal with — this never recurses or removes a tree. A dangling
    symlink unlinks cleanly, which is the right outcome: the link is the
    stale artefact.

    Returns:
      The paths removed, sorted, for logging.
    """
    directory = Path(artefact_dir)
    suffixes = tuple(suffixes)
    keep = set(keep)
    try:
        entries = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    doomed = [
        entry
        for entry in entries
        if entry.name not in keep
        and entry.name.endswith(suffixes)
        and not any(fnmatch(entry.name, pat) for pat in PROTECTED_OUTPUT_PATTERNS)
    ]
    return clear_stale_artefacts(doomed, owner=owner)


test_artifact_dir.__test__ = False
test_build_dir_name.__test__ = False
