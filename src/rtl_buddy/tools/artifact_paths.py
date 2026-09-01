from pathlib import Path
import re

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


test_artifact_dir.__test__ = False
test_build_dir_name.__test__ = False
