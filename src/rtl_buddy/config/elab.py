"""Configuration views for model elaboration and elaboration regressions."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from serde import field, serde
from serde.yaml import from_yaml

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .dispatch import DispatchResourcesFile
from .model import (
    ELAB_BASE_ARTIFACT_NAME,
    ElaborationProfile,
    ModelConfig,
    ModelConfigLoader,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ElabConfig:
    """One model plus either its base settings or one named profile."""

    model: ModelConfig
    profile: ElaborationProfile | None = None

    @property
    def name(self) -> str:
        if self.profile is None:
            return self.model.name
        return f"{self.model.name}:{self.profile.name}"

    @property
    def profile_name(self) -> str | None:
        return self.profile.name if self.profile is not None else None

    @property
    def top(self) -> str:
        if self.profile is None:
            return self.model.get_top()
        return self.profile.get_top(self.model)

    @property
    def reglvl(self) -> int:
        return self.profile.reglvl if self.profile is not None else 0

    @property
    def resources(self) -> DispatchResourcesFile | None:
        return self.profile.resources if self.profile is not None else None

    @property
    def config_dir(self) -> Path:
        if self.model.path is None:
            raise FatalRtlBuddyError(
                f"model {self.model.name!r} is not bound to a models.yaml path"
            )
        return Path(self.model.path).resolve().parent

    @property
    def artifact_dir(self) -> Path:
        leaf = self.profile_name or ELAB_BASE_ARTIFACT_NAME
        return self.config_dir / "artefacts" / "elab" / self.model.name / leaf

    def resolve_profile_path(self, value: str) -> str:
        path = Path(value)
        if path.is_absolute():
            return str(path)
        return str((self.config_dir / path).resolve())


@serde
class ElabRegConfigFile:
    rtl_buddy_filetype: Literal["elab_reg_config"] = field(rename="rtl-buddy-filetype")
    model_configs: list[str] = field(rename="model-configs", default_factory=list)


class ElabRegConfig:
    """An explicit manifest of ``models.yaml`` files used by a regression."""

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = str(Path(path).resolve())
        try:
            raw = Path(self.path).read_text()
            data = from_yaml(ElabRegConfigFile, raw)
            base = Path(self.path).parent
            paths = [str((base / item).resolve()) for item in data.model_configs]
            if not paths:
                raise FatalRtlBuddyError(
                    f"{self.path}: model-configs must name at least one models.yaml"
                )
            if len(paths) != len(set(paths)):
                raise FatalRtlBuddyError(
                    f"{self.path}: model-configs contains a duplicate path"
                )
            self.model_loaders = [ModelConfigLoader(item) for item in paths]
        except FatalRtlBuddyError:
            raise
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "elab_regression_config.load_failed",
                name=name,
                path=self.path,
                error=exc,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from exc

        if not any(
            model.elaborations
            for loader in self.model_loaders
            for model in loader.get_models()
        ):
            raise FatalRtlBuddyError(
                f"{self.path}: elaboration regression has no named profiles"
            )

    def get_elaborations(self) -> list[ElabConfig]:
        return [
            ElabConfig(model=model, profile=profile)
            for loader in self.model_loaders
            for model in loader.get_models()
            for profile in model.elaborations
        ]
