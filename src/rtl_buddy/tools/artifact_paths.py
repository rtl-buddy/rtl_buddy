from pathlib import Path
import re


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
        Path(suite_dir) / "artefacts" / sanitize_artifact_component(test_name)
    )
    if run_id is not None:
        artifact_dir /= f"run-{run_id:04d}"
    return artifact_dir


def test_build_dir_name(test_name: str) -> str:
    """
    Return the simulator build directory name for a test.
    """
    return f"obj_dir_{sanitize_artifact_component(test_name)}"


def shared_build_dir(suite_dir: str | Path, compile_key: str) -> Path:
    """
    Return the compile-input-keyed build directory shared by all tests in a
    suite whose compile inputs hash to ``compile_key``.

    Lives under a dot-directory so it can never collide with a per-test
    artifact directory derived from a test name.
    """
    return Path(suite_dir) / "artefacts" / ".shared-builds" / f"obj_dir_{compile_key}"


test_artifact_dir.__test__ = False
test_build_dir_name.__test__ = False
