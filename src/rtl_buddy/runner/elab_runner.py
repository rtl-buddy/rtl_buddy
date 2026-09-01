"""Run one model elaboration in an isolated pyslang worker."""

import shlex
import subprocess
import sys
from pathlib import Path

from ..config.dispatch import JobResources
from ..config.elab import ElabConfig
from ..errors import FatalRtlBuddyError
from ..process_utils import run_managed_process
from ..tool_manifest import require
from ..tools.vlog_filelist import VlogFilelist
from .elab_results import (
    elab_failure,
    load_elab_result_json,
    write_elab_result_json,
)


def _render_value(value: int | bool | str) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


class ElabRunner:
    def __init__(
        self,
        *,
        root_cfg,
        elab_cfg: ElabConfig,
        resources: JobResources,
        result_json: str | Path | None = None,
    ) -> None:
        self.root_cfg = root_cfg
        self.elab_cfg = elab_cfg
        self.resources = resources
        self.artifact_dir = elab_cfg.artifact_dir
        self.filelist_path = self.artifact_dir / "elab.f"
        self.log_path = self.artifact_dir / "elab.log"
        self.durable_result_path = self.artifact_dir / "result.json"
        self.worker_result_path = (
            Path(result_json).resolve()
            if result_json is not None
            else self.durable_result_path
        )

    def _slang_args(self) -> list[str]:
        profile = self.elab_cfg.profile
        args = [
            f"--top={self.elab_cfg.top}",
            f"-j={self.resources.cpus}",
            f"-f={self.filelist_path}",
        ]
        if profile is None:
            return args
        if profile.vcs_compat:
            args.append("--compat=vcs")
        if profile.single_unit:
            args.append("--single-unit")
        if profile.libraries_inherit_macros:
            args.append("--libraries-inherit-macros")
        if profile.timescale is not None:
            args.append(f"--timescale={profile.timescale}")
        args.extend(f"--ignore-directive={item}" for item in profile.ignored_directives)
        args.extend(f"-W{item}" for item in profile.warnings)
        args.extend(
            f"-G{name}={_render_value(value)}"
            for name, value in profile.parameters.items()
        )
        return args

    def run(self):
        require("pyslang", self.root_cfg)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        filelist = VlogFilelist(
            "elab/filelist", self.elab_cfg.model, str(self.filelist_path)
        )
        input_source_count = filelist.write_elab_output(
            self.elab_cfg, str(self.filelist_path)
        )
        cmd = [
            sys.executable,
            "-m",
            "rtl_buddy.elab_worker",
            "--result-json",
            str(self.worker_result_path),
            "--model",
            self.elab_cfg.model.name,
            "--top",
            self.elab_cfg.top,
            "--input-source-count",
            str(input_source_count),
        ]
        if self.elab_cfg.profile_name is not None:
            cmd += ["--profile", self.elab_cfg.profile_name]
        cmd += ["--", *self._slang_args()]

        write_elab_result_json(
            self.worker_result_path,
            model=self.elab_cfg.model.name,
            profile=self.elab_cfg.profile_name,
            results=elab_failure("elaboration worker did not produce a result"),
        )
        with self.log_path.open("w") as log:
            log.write(f"Command: {shlex.join(cmd)}\n\n")
            log.flush()
            proc = run_managed_process(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(self.artifact_dir),
            )

        try:
            worker_result = load_elab_result_json(
                self.worker_result_path,
                model=self.elab_cfg.model.name,
                profile=self.elab_cfg.profile_name,
            )
        except FatalRtlBuddyError as exc:
            results = elab_failure(
                f"elaboration worker exited with code {proc.returncode} without a valid result: {exc}"
            )
            write_elab_result_json(
                self.worker_result_path,
                model=self.elab_cfg.model.name,
                profile=self.elab_cfg.profile_name,
                results=results,
            )
            worker_result = load_elab_result_json(
                self.worker_result_path,
                model=self.elab_cfg.model.name,
                profile=self.elab_cfg.profile_name,
            )

        if self.worker_result_path != self.durable_result_path:
            write_elab_result_json(
                self.durable_result_path,
                model=self.elab_cfg.model.name,
                profile=self.elab_cfg.profile_name,
                results=worker_result.results,
            )
        return load_elab_result_json(
            self.durable_result_path,
            model=self.elab_cfg.model.name,
            profile=self.elab_cfg.profile_name,
        )
