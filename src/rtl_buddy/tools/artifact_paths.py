import hashlib
import json
import os
import uuid
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable
import re

from ..errors import FatalRtlBuddyError

#: The suite-relative directory every rtl_buddy artefact tree is written into.
ARTIFACT_DIRNAME = "artefacts"

#: Holds the compile-key-named shared build directories, under :data:`ARTIFACT_DIRNAME`.
SHARED_BUILDS_DIRNAME = ".shared-builds"

#: Holds one subdirectory per ``--run-tag``, under :data:`ARTIFACT_DIRNAME`. A tagged run's
#: whole artefact tree moves to ``artefacts/.runs/<tag>/``. It is a dot directory because
#: readers of the artefact tree skip dot names; a tag beside the per-test directories
#: would be mistaken for a test.
RUNS_DIRNAME = ".runs"

#: What a ``--run-tag`` may contain: one safe path segment with no separators.
RUN_TAG_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

#: Longest accepted tag; short enough to leave room for the per-test and ``run-NNNN`` components below it.
RUN_TAG_MAX_LEN = 64

#: Prefix of every simulator build directory (``obj_dir_<test>`` and ``obj_dir_<key>``).
BUILD_DIR_PREFIX = "obj_dir"

#: The per-run result envelope written into a test's artefact directory.
RESULT_JSON_NAME = "result.json"

#: The compile stamp a simulator build writes beside its build directory (into
#: ``artefacts/<test>/`` for an unshared build). Defined here, not in :mod:`.vlog_sim`,
#: so the protected set below and the writer share one name.
SHARED_BUILD_STAMP_NAME = "rb-compile-stamp.json"

#: The graph tier's durable exports in ``artefacts/graph/``. Defined here because this
#: module is at the bottom of the import graph.
GRAPH_JSON_NAME = "graph.json"
GRAPH_META_NAME = "graph-meta.json"
RESULTS_OVERLAY_NAME = "results-overlay.json"

#: `rb cov`'s durable outputs, written side by side in the coverage
#: directory (``artefacts/cov_dir/`` by default).
COV_MANIFEST_NAME = "manifest.json"
COV_MODEL_NAME = "coverage-model.json"

#: The physical-metrics model and manifest, written into the producing run's artefact
#: directory (`rb synth` or `rb power`). The manifest is prefixed because discovery
#: matches on filename and must not read a coverage manifest as a physical one.
PHYS_MANIFEST_NAME = "phys-manifest.json"
PHYS_MODEL_NAME = "phys-model.json"

#: The lock a publisher holds while it reads, merges and rewrites the physical-metrics
#: pair. Co-named `rb synth` and `rb power` runs each write both documents into one directory.
PHYS_PUBLISH_LOCK_NAME = "phys-publish.lock"

#: `rb xplr`'s per-experiment ledger record and its git provenance sidecar,
#: in ``artefacts/xplr/<exp-id>/``.
XPLR_RECORD_NAME = "record.json"
XPLR_WORKTREE_SIDECAR_NAME = "worktree.json"

#: A dispatched job's envelope and log, in ``<test>/dispatch/`` and ``artefacts/.dispatch/``
#: (see :mod:`rtl_buddy.dispatch.argv`). fnmatch patterns, since both carry a per-job tag.
DISPATCH_OUTPUT_PATTERNS = (
    "result-*.json",
    "build-result-*.json",
    "rtl_buddy-*.log",
    "build-rtl_buddy-*.log",
)

#: Fixed-name durable outputs that other commands write into ``artefacts/<name>/`` and read
#: back later. Artefact directories are keyed on a run's name, and names can repeat across
#: commands (a CDC analysis and an FPGA run with the same name), so a suffix clear must
#: not remove these. Only fixed names belong here: a flow clears its own fixed-name outputs
#: through :func:`clear_stale_artefacts`, which ignores this set. ``tests/test_vlog_sim_paths.py``
#: checks that each name is written by a flow and is not cleared by suffix.
SIBLING_OUTPUT_NAMES = (
    # rb test's build cache (tools/vlog_sim.py)
    SHARED_BUILD_STAMP_NAME,
    # rb cdc, open analyzer (tools/cdc_rtl_buddy.py)
    "cdc.json",
    "cdc.txt",
    "domain_map.json",
    "reset_map.json",
    # rb cdc, Vivado backend (tools/cdc_vivado.py)
    "cdc.rpt",
    # rb power (tools/power_openroad.py)
    "power.rpt",
    # The run's private copy of the upstream netlist, as read by OpenROAD.
    "power_netlist.v",
    # `.cells` is the instance -> liberty-cell map that `report_power` does not print.
    "power_instances.rpt",
    "power_instances.cells",
    # rb pnr's design-independent reports (tools/pnr_openroad.py)
    "route.drc.rpt",
    "timing.rpt",
    # rb pnr-export: provenance of the exported layout; nothing regenerates it.
    "export.provenance.json",
    # Stream-out input manifest and KLayout completeness report; a strict re-render reads the report.
    "def2stream.inputs.json",
    "def2stream.report.json",
    # rb synth (tools/synth_yosys.py, tools/synth_openroad.py)
    "synth_netlist.v",
    "synth.rtlil",
    # Yosys `stat -json` dump, the source of the phys model's module rows.
    "synth_stat.json",
    # rb axi-profile: in its own `artefacts/axi/<name>/` subtree, listed in case a flow globs there.
    "axi-perf.json",
    # rb graph: directly in `artefacts/graph/`, which an FPGA run named `graph` shares.
    GRAPH_JSON_NAME,
    GRAPH_META_NAME,
    RESULTS_OVERLAY_NAME,
    # rb cov: directly in the coverage directory, which is user-supplied.
    COV_MANIFEST_NAME,
    COV_MODEL_NAME,
    # rb synth / rb power: directly in the producing run's `artefacts/<name>/`.
    PHYS_MANIFEST_NAME,
    PHYS_MODEL_NAME,
    PHYS_PUBLISH_LOCK_NAME,
    # rb xplr: in `artefacts/xplr/<exp-id>/`, listed in case the layout changes.
    XPLR_RECORD_NAME,
    XPLR_WORKTREE_SIDECAR_NAME,
)

#: Everything :func:`clear_managed_outputs` must never remove: the result envelope,
#: dispatch outputs and the sibling commands' outputs above.
PROTECTED_OUTPUT_PATTERNS = (
    RESULT_JSON_NAME,
    *DISPATCH_OUTPUT_PATTERNS,
    *SIBLING_OUTPUT_NAMES,
)

#: Where a flow records the output names it has claimed in an artefact directory. A dotfile, so no suffix clear matches it.
OWNED_LEDGER_NAME = ".rb-owned"

#: Suffix of the intermediate file every atomic write renames into place.
ATOMIC_TMP_SUFFIX = ".tmp"


def atomic_tmp_name(name) -> str:
    """Return the temporary name ``<name>.<pid>.<uuid4 hex>.tmp`` for an atomic write to ``name``.

    The caller renames it over ``name`` with :func:`os.replace`. The pid identifies a
    leaked file and the uuid keeps concurrent writers apart. Managed outputs build their
    temp names here so :func:`atomic_tmp_patterns` can exclude them.
    """
    return f"{name}.{os.getpid()}.{uuid.uuid4().hex}{ATOMIC_TMP_SUFFIX}"


def atomic_tmp_patterns(pattern: str) -> tuple[str, ...]:
    """fnmatch patterns for the in-flight temp files of the output ``pattern``.

    Matches ``<name>.tmp`` and the ``<name>.<pid>.<uuid>.tmp`` form of :func:`atomic_tmp_name`.
    It is not a blanket ``*.tmp``, because a project's own ``defs.tmp`` is a compile input.
    """
    return (
        f"{pattern}{ATOMIC_TMP_SUFFIX}",
        f"{pattern}.*{ATOMIC_TMP_SUFFIX}",
    )


def project_relative(path, project_root) -> str | None:
    """Return the POSIX path relative to the project root, or ``str(path)`` when it is outside the project.

    Manifest paths use this so they stay valid after the tree is moved or archived.
    Logical (unresolved) paths are compared first, then resolved ones, so an
    ``artefacts/`` symlinked to scratch storage still yields project-relative paths.
    Re-exported from :mod:`rtl_buddy.phys.manifest` and :mod:`rtl_buddy.cov.manifest`.
    """
    if path is None:
        return None
    logical = Path(os.path.abspath(path))
    logical_root = Path(os.path.abspath(project_root))
    try:
        return logical.relative_to(logical_root).as_posix()
    except ValueError:
        pass
    try:
        return Path(path).resolve().relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return str(path)


#: Project root markers, in the order :func:`rtl_buddy.config.root.discover_project_root` uses.
ROOT_MARKERS = ("root_config.yaml", ".git")


def project_root_or_none(artefact_dir) -> str | None:
    """Return the project root above ``artefact_dir``, or ``None`` if it is in none.

    Unlike :func:`rtl_buddy.config.root.discover_project_root` this does not log an error
    for a directory outside a project. Logical and resolved paths are tried in that order,
    as in :func:`project_relative`.
    """
    logical = Path(os.path.abspath(artefact_dir))
    for start in (logical, Path(artefact_dir).resolve()):
        for candidate in (start, *start.parents):
            if any((candidate / marker).exists() for marker in ROOT_MARKERS):
                return str(candidate)
    return None


def joins_back(root, artefact_dir_rel: str, artefact_dir_abs) -> bool:
    """Whether ``root / artefact_dir_rel`` resolves to ``artefact_dir_abs``.

    Verifies the root derived from a manifest's component count (each manifest module's ``project_root_for``).
    """
    joined = os.path.join(str(root), artefact_dir_rel)
    return os.path.realpath(joined) == os.path.realpath(artefact_dir_abs)


def read_owned_ledger(artefact_dir: str | Path, flow: str) -> set[str]:
    """Return the output names ``flow`` has previously claimed in ``artefact_dir``.

    The ledger records what a flow wrote, so leftovers named after an earlier top stay
    clearable. It is keyed by flow because commands sharing a run name share the directory
    and must not inherit each other's claims. A missing, unreadable or unrecognised ledger
    is an empty claim.
    """
    mapping = _read_owned_ledger_mapping(artefact_dir)
    return set(mapping.get(flow, ()))


def _read_owned_ledger_mapping(artefact_dir: str | Path) -> dict[str, list[str]]:
    """Return the whole ``{flow: [names]}`` ledger, or ``{}``."""
    path = Path(artefact_dir) / OWNED_LEDGER_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        flow: [n for n in names if isinstance(n, str)]
        for flow, names in raw.items()
        if isinstance(flow, str) and isinstance(names, list)
    }


def write_owned_ledger(
    artefact_dir: str | Path, flow: str, names: Iterable[str]
) -> None:
    """Record ``names`` as owned by ``flow`` in this directory, keeping other flows' claims.

    Best-effort: a write failure is ignored, and the only cost is that a later rename leaves a file behind.
    """
    path = Path(artefact_dir) / OWNED_LEDGER_NAME
    mapping = _read_owned_ledger_mapping(artefact_dir)
    mapping[flow] = sorted(set(names))
    try:
        path.write_text(
            json.dumps({k: mapping[k] for k in sorted(mapping)}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def sanitize_artifact_component(name: str) -> str:
    """Return ``name`` with characters outside ``A-Za-z0-9_.-`` replaced by ``_``."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def validate_run_tag(run_tag: str | None) -> str | None:
    """Return ``run_tag`` unchanged, or raise; ``None`` passes through.

    A tag names a directory, so it is rejected rather than sanitized, which would let two
    tags map to one tree. Validate once at the CLI boundary and pass the value on.

    Raises:
      FatalRtlBuddyError: the tag is empty, too long, not a safe path segment, or only dots.
    """
    if run_tag is None:
        return None
    tag = str(run_tag)
    if not tag:
        raise FatalRtlBuddyError("--run-tag cannot be empty")
    if len(tag) > RUN_TAG_MAX_LEN:
        raise FatalRtlBuddyError(
            f"--run-tag {tag!r} is longer than {RUN_TAG_MAX_LEN} characters"
        )
    if not RUN_TAG_PATTERN.fullmatch(tag):
        raise FatalRtlBuddyError(
            f"--run-tag {tag!r} is not a safe path segment: use only "
            "letters, digits, '.', '_' and '-'"
        )
    if set(tag) == {"."}:
        # '.' and '..' would address the parent tree.
        raise FatalRtlBuddyError(f"--run-tag {tag!r} is not a directory name")
    return tag


def run_artifact_root(suite_dir: str | Path, run_tag: str | None = None) -> Path:
    """Return the artefact tree one invocation writes into.

    ``<suite>/artefacts``, or ``<suite>/artefacts/.runs/<tag>`` with a run tag. The
    per-test directories, tree lock, dispatch outputs and results overlay live under it.
    Shared builds do not (:func:`shared_build_dir`), so tagged runs reuse compiled builds.
    """
    root = Path(suite_dir) / ARTIFACT_DIRNAME
    if run_tag is None:
        return root
    return root / RUNS_DIRNAME / validate_run_tag(run_tag)


def test_artifact_dir(
    suite_dir: str | Path,
    test_name: str,
    run_id: int | None = None,
    run_tag: str | None = None,
) -> Path:
    """Return the per-test artifact directory, with a ``run-NNNN`` subdirectory when ``run_id`` is given."""
    artifact_dir = run_artifact_root(suite_dir, run_tag) / sanitize_artifact_component(
        test_name
    )
    if run_id is not None:
        artifact_dir /= f"run-{run_id:04d}"
    return artifact_dir


def test_build_dir_name(test_name: str) -> str:
    """Return the simulator build directory name for a test."""
    return f"{BUILD_DIR_PREFIX}_{sanitize_artifact_component(test_name)}"


#: Namespace of a suite that is the project root.
_ROOT_SUITE_NAMESPACE = "_root"


def shared_build_namespace(suite_dir: str | Path, project_root: str | Path) -> str:
    """Return the per-suite directory name inside a persistent build cache root.

    It is the suite path relative to the project root with ``/`` replaced by ``__``
    (``verif/demo_tiny_alu`` -> ``verif__demo_tiny_alu``). It does not depend on the
    checkout, so workspaces of one project share builds; the compile key separates
    different content. A suite outside the project root gets a digest of its absolute path.
    """
    suite = os.path.realpath(str(suite_dir))
    root = os.path.realpath(str(project_root)) if project_root else None
    if root is not None:
        try:
            relative = os.path.relpath(suite, root)
        except ValueError:
            relative = os.pardir
        if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
            parts = [part for part in Path(relative).parts if part != os.curdir]
            return (
                "__".join(sanitize_artifact_component(part) for part in parts)
                or _ROOT_SUITE_NAMESPACE
            )
    return hashlib.sha256(suite.encode("utf-8")).hexdigest()[:12]


def shared_build_dir(
    suite_dir: str | Path,
    compile_key: str,
    *,
    cache_root: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Path:
    """Return the build directory shared by all tests of a suite whose compile inputs hash to ``compile_key``.

    By default it is ``artefacts/.shared-builds/obj_dir_<key>``, a dot directory that cannot
    collide with a per-test directory. ``cache_root`` moves it into a persistent cache, under
    a per-suite namespace.
    """
    if cache_root is not None:
        return (
            Path(cache_root)
            / shared_build_namespace(suite_dir, project_root)
            / f"{BUILD_DIR_PREFIX}_{compile_key}"
        )
    return (
        Path(suite_dir)
        / ARTIFACT_DIRNAME
        / SHARED_BUILDS_DIRNAME
        / f"{BUILD_DIR_PREFIX}_{compile_key}"
    )


def clear_stale_artefacts(
    paths: Iterable[str | Path | None], *, owner: str
) -> list[str]:
    """Delete a tool's outputs before the tool runs, so that a file present afterwards was written by this run.

    Flows read outputs from fixed paths, and an exit code cannot tell a clean run from a
    crash that exits with a "violations found" code. Logs are not passed here; the log is
    worth keeping if the tool dies.

    Call it early. Outputs that a later command consumes (synthesis netlists, pnr DEF and
    ODB) are cleared first in ``run()``, before any validation or tool check. Outputs
    read within the same ``run()`` (CDC, FPGA and power reports, bitstreams) are cleared
    right after the tool-availability skip, so a host without the tool deletes nothing.

    Args:
      paths: outputs this run will rewrite; missing and ``None`` entries are ignored.
      owner: the run or analysis name, for the error message.

    Returns:
      The paths that existed, in the order given.

    Raises:
      FatalRtlBuddyError: an existing artefact could not be removed.
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
    own: Iterable[str] = (),
    own_flow: str | None = None,
    keep: Iterable[str] = (),
) -> list[str]:
    """Clear a run's outputs by suffix, for flows whose output names contain the design's top.

    An artefact directory belongs to one run, so any file with a managed suffix is that
    run's output, whatever the current top is. Only the directory itself is scanned, not
    subdirectories. Matching entries that are not regular files (a directory named
    ``<top>.bit``) are passed to :func:`clear_stale_artefacts`, which cannot remove a
    directory and so fails with a message naming the path; a dangling symlink is removed.

    Args:
      artefact_dir: the run's artefact directory. A missing directory is not an error.
      suffixes: filename suffixes the flow writes, with the dot (``".bit"``,
        ``".routed.odb"``). Do not include a log suffix.
      owner: the run or analysis name, for error messages.
      own: exact filenames this run writes. They are cleared even when they match a
        protected pattern, together with the names ``own_flow`` claimed on earlier runs
        (:func:`read_owned_ledger`), so a changed top leaves nothing behind.
      own_flow: identity of the producing flow, e.g. ``"fpga-openxc7"``. It enables the
        per-flow ledger. When ``own`` is non-empty, the ledger is replaced by this run's
        ``own``, which retires inherited names once cleared; with an empty ``own`` the
        existing claim is kept. Without ``own_flow``, ``own`` applies to this call only.
      keep: exact filenames to leave alone even when they match a suffix.
        :data:`PROTECTED_OUTPUT_PATTERNS` are always kept. ``own`` overrides both.

    Returns:
      The paths removed, sorted.

    Raises:
      FatalRtlBuddyError: the directory could not be listed or an entry could not be removed.
    """
    directory = Path(artefact_dir)
    suffixes = tuple(suffixes)
    declared = set(own)
    own = declared | (
        read_owned_ledger(directory, own_flow) if own_flow is not None else set()
    )
    keep = set(keep) - own
    try:
        entries = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        # An unlistable directory is not an empty one; stale outputs would survive.
        raise FatalRtlBuddyError(
            f"{owner}: could not list the previous run's artefacts in {directory}: {e}"
        ) from e

    def _doomed(name: str) -> bool:
        if name in own:
            return True
        if name in keep:
            return False
        return name.endswith(suffixes) and not any(
            fnmatch(name, pat) for pat in PROTECTED_OUTPUT_PATTERNS
        )

    removed = clear_stale_artefacts(
        [entry for entry in entries if _doomed(entry.name)], owner=owner
    )
    if declared and own_flow is not None:
        # Record only this run's names, not the union used above, or an old top such as
        # `graph` would keep `graph.json` clearable forever.
        write_owned_ledger(directory, own_flow, declared)
    return removed


test_artifact_dir.__test__ = False
test_build_dir_name.__test__ = False
