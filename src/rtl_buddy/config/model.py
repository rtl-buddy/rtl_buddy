import logging
import os
import re

logger = logging.getLogger(__name__)
import pprint

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal

from .dispatch import DispatchResourcesFile, validate_resources_block
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

#: A model ``name:`` must be safe as a single path segment: it names artefact directories, and ``rb graph build`` deletes ``design/<name>/``.
#: Slightly wider than a SystemVerilog identifier (allows ``-`` and ``.``) but never starts with ``.`` and never contains a separator.
#: Anchored with ``\\Z`` because ``$`` also matches before a trailing newline.
MODEL_NAME_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def validate_model_name(name: str, path: str) -> None:
    """Raise ``FatalRtlBuddyError`` unless ``name`` is safe as an artefact directory name.

    ``path`` is the models.yaml the name came from, used in the message.
    """
    if isinstance(name, str) and MODEL_NAME_RE.match(name):
        return
    log_event(
        logger,
        logging.ERROR,
        "model_config.invalid_model_name",
        path=path,
        name=name,
    )
    raise FatalRtlBuddyError(
        f"{path}: model name {name!r} is not usable — a model name becomes "
        f"a directory under artefacts/ (and its default top module), so it "
        f"must start with a letter, digit or underscore and contain only "
        f"letters, digits, underscore, dot or hyphen. Path separators, "
        f"absolute paths, '.' and '..' are refused."
    )


#: A model ``top:`` must be a simple SystemVerilog identifier.
#: It is interpolated unquoted into artefact paths and generated Tcl, so it must be inert in both.
#: ``$`` is legal SystemVerilog but excluded because Tcl substitutes it; escaped identifiers are refused.
#: ``get_top()`` falls back to the model name, and :data:`MODEL_NAME_RE` is safe in the same places.
MODEL_TOP_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
ELAB_TIMESCALE_RE = re.compile(r"\A\d+(?:s|ms|us|ns|ps|fs)/\d+(?:s|ms|us|ns|ps|fs)\Z")
ELAB_WARNING_RE = re.compile(
    r"\A(?:none|all|error|no-[A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"error=[A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"no-error=[A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"[A-Za-z0-9_][A-Za-z0-9_-]*)\Z"
)
ELAB_BASE_ARTIFACT_NAME = "base"

#: Upper bound for an elaboration profile's ``max_parse_depth``, 64x slang's default of 1024.
#: Deeper nesting can exhaust the C stack and kill the worker with no diagnostic, so an absurd value is a load-time error.
ELAB_MAX_PARSE_DEPTH_LIMIT = 65536


def validate_top(
    top: str,
    name: str,
    path: str,
    *,
    subject: str = "model",
    event: str = "model_config.invalid_model_top",
) -> None:
    """Raise ``FatalRtlBuddyError`` unless ``top`` is a simple SystemVerilog identifier.

    ``fpv.yaml`` and ``mut.yaml`` validate their own ``top:`` with this rule too.
    ``name``, ``path`` and ``subject`` identify the declarer in the message; ``event`` is the log event name.
    """
    if isinstance(top, str) and MODEL_TOP_RE.match(top):
        return
    escaped = isinstance(top, str) and top.startswith("\\")
    dollar = isinstance(top, str) and "$" in top and not escaped
    log_event(
        logger,
        logging.ERROR,
        event,
        path=path,
        name=name,
        top=top,
    )
    if escaped:
        detail = (
            "SystemVerilog escaped identifiers are refused here: no flow can "
            "name an artefact file or a Tcl token after one safely."
        )
    elif dollar:
        detail = (
            "'$' is legal in SystemVerilog but is a substitution character "
            "in the Vivado and OpenROAD Tcl this value is written into "
            "unquoted, so `synth_design -top` would elaborate a different "
            "name than the one declared here. Rename the module, or wrap it "
            "in one whose name has no '$'."
        )
    else:
        detail = (
            "It must start with a letter or underscore and contain only "
            "letters, digits or underscore."
        )
    raise FatalRtlBuddyError(
        f"{path}: {subject} {name!r} declares top {top!r}, which is not a "
        f"simple SystemVerilog identifier. {detail} The top is elaborated by "
        f"every backend and also lands in artefact names and generated Tcl, "
        f"so a path separator, newline or shell/Tcl metacharacter is refused."
    )


def validate_model_top(top: str, name: str, path: str) -> None:
    """Validate a models.yaml ``top:`` with :func:`validate_top`."""
    validate_top(top, name, path)


def split_back_pointer(value: str) -> tuple[str, str | None]:
    """Split a ``cdc:``/``synth:``/``tests:`` back-pointer into ``(path, entry_name | None)``.

    The optional ``#entry_name`` fragment picks one entry from a multi-entry file.
    """
    if "#" in value:
        path, _, entry = value.partition("#")
        entry = entry.strip()
        return path, (entry if entry else None)
    return value, None


def resolve_back_pointer(
    model: "ModelConfig", field_name: str
) -> tuple[str, str | None] | None:
    """Resolve ``model.<field_name>`` (``cdc``, ``synth`` or ``tests``) into an absolute ``(path, entry_name | None)``.

    Returns ``None`` when the field is unset. Raises ``FatalRtlBuddyError`` when it is set but ``model.path`` is missing.
    """
    raw = getattr(model, field_name, None)
    if not raw:
        return None
    if not model.path:
        raise FatalRtlBuddyError(
            f"resolve_back_pointer: model {model.name!r} has no path "
            f"attribute; cannot resolve {field_name}={raw!r}"
        )
    rel, entry = split_back_pointer(raw)
    return model._resolve_relative(rel), entry


@serde
class ElaborationProfile:
    """Optional elaboration overrides nested in one ``models.yaml`` model."""

    name: str
    desc: str | None = None
    top: str | None = None
    reglvl: int = 0
    prepend_sources: list[str] = field(default_factory=list)
    append_sources: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    defines: dict[str, int | bool | str | None] = field(default_factory=dict)
    parameters: dict[str, int | bool | str] = field(default_factory=dict)
    vcs_compat: bool = False
    single_unit: bool = False
    libraries_inherit_macros: bool = False
    timescale: str | None = None
    max_parse_depth: int | None = None
    ignored_directives: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resources: DispatchResourcesFile | None = None

    def get_top(self, model: "ModelConfig") -> str:
        return self.top or model.get_top()


def _validate_elaboration_profile(
    profile: ElaborationProfile, model: "ModelConfig", path: str
) -> None:
    prefix = f"{path}: model {model.name!r} elaboration {profile.name!r}"
    if not isinstance(profile.name, str) or not MODEL_NAME_RE.match(profile.name):
        raise FatalRtlBuddyError(
            f"{prefix} has an invalid name; profile names must be safe single "
            "path segments containing only letters, digits, underscore, dot or hyphen"
        )
    if profile.name.casefold() == ELAB_BASE_ARTIFACT_NAME.casefold():
        raise FatalRtlBuddyError(
            f"{prefix} uses reserved name {ELAB_BASE_ARTIFACT_NAME!r}; that "
            "directory records a bare-model elaboration"
        )
    if profile.desc is not None and not isinstance(profile.desc, str):
        raise FatalRtlBuddyError(f"{prefix} desc must be a string or null")
    if profile.top is not None:
        if not isinstance(profile.top, str):
            raise FatalRtlBuddyError(f"{prefix} top must be a simple identifier")
        validate_model_top(profile.top, f"{model.name}:{profile.name}", path)
    if not isinstance(profile.reglvl, int) or isinstance(profile.reglvl, bool):
        raise FatalRtlBuddyError(f"{prefix} reglvl must be a non-negative integer")
    if profile.reglvl < 0:
        raise FatalRtlBuddyError(f"{prefix} reglvl must be a non-negative integer")
    for option in ("vcs_compat", "single_unit", "libraries_inherit_macros"):
        if not isinstance(getattr(profile, option), bool):
            raise FatalRtlBuddyError(f"{prefix} {option} must be a boolean")
    if profile.libraries_inherit_macros and not profile.single_unit:
        raise FatalRtlBuddyError(
            f"{prefix} enables libraries_inherit_macros without single_unit; "
            "slang requires both options together"
        )
    if profile.timescale is not None:
        if not isinstance(profile.timescale, str) or not ELAB_TIMESCALE_RE.match(
            profile.timescale
        ):
            raise FatalRtlBuddyError(
                f"{prefix} timescale {profile.timescale!r} must look like '1ns/1ps'"
            )
    if profile.max_parse_depth is not None:
        depth = profile.max_parse_depth
        if (
            not isinstance(depth, int)
            or isinstance(depth, bool)
            or not 1 <= depth <= ELAB_MAX_PARSE_DEPTH_LIMIT
        ):
            raise FatalRtlBuddyError(
                f"{prefix} max_parse_depth {depth!r} must be an integer between "
                f"1 and {ELAB_MAX_PARSE_DEPTH_LIMIT}"
            )
    for option in ("prepend_sources", "append_sources", "include_dirs"):
        values = getattr(profile, option)
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            raise FatalRtlBuddyError(f"{prefix} {option} must be a list of strings")
    if not isinstance(profile.defines, dict):
        raise FatalRtlBuddyError(f"{prefix} defines must be a mapping")
    for key, value in profile.defines.items():
        if not isinstance(key, str) or not MODEL_TOP_RE.match(key):
            raise FatalRtlBuddyError(
                f"{prefix} define name {key!r} is not a simple identifier"
            )
        if value is not None and not isinstance(value, (str, int, bool)):
            raise FatalRtlBuddyError(
                f"{prefix} define {key!r} must be a string, integer, boolean or null"
            )
        if isinstance(value, str) and (
            not value or "+" in value or any(char.isspace() for char in value)
        ):
            raise FatalRtlBuddyError(
                f"{prefix} define {key!r} has an invalid value; string values "
                "cannot be empty or contain whitespace or '+'"
            )
    if not isinstance(profile.parameters, dict):
        raise FatalRtlBuddyError(f"{prefix} parameters must be a mapping")
    for key, value in profile.parameters.items():
        if not isinstance(key, str) or not MODEL_TOP_RE.match(key):
            raise FatalRtlBuddyError(
                f"{prefix} parameter name {key!r} is not a simple identifier"
            )
        if not isinstance(value, (str, int, bool)):
            raise FatalRtlBuddyError(
                f"{prefix} parameter {key!r} must be a string, integer or boolean"
            )
        rendered = "1" if value is True else "0" if value is False else str(value)
        if not rendered or "\n" in rendered or "\x00" in rendered:
            raise FatalRtlBuddyError(f"{prefix} parameter {key!r} has an invalid value")
    if not isinstance(profile.ignored_directives, list):
        raise FatalRtlBuddyError(
            f"{prefix} ignored_directives must be a list of identifiers"
        )
    for directive in profile.ignored_directives:
        if not isinstance(directive, str) or not MODEL_TOP_RE.match(directive):
            raise FatalRtlBuddyError(
                f"{prefix} ignored directive {directive!r} is not an identifier"
            )
    if not isinstance(profile.warnings, list):
        raise FatalRtlBuddyError(f"{prefix} warnings must be a list of controls")
    for warning in profile.warnings:
        if not isinstance(warning, str) or not ELAB_WARNING_RE.match(warning):
            raise FatalRtlBuddyError(
                f"{prefix} warning control {warning!r} is invalid; write the part "
                "after '-W', for example 'all', 'no-unused' or 'error=unused'"
            )
    # An elaboration reservation has no builder mode, so a `modes:` block is refused.
    profile.resources = validate_resources_block(profile.resources, where=f"{prefix} ")
    if profile.resources is not None and profile.resources.cpus is not None:
        cpus = profile.resources.cpus
        if not isinstance(cpus, int) or isinstance(cpus, bool) or cpus < 1:
            raise FatalRtlBuddyError(
                f"{prefix} resources.cpus must be a positive integer"
            )


@serde
class ModelConfig:
    """One model entry in a ``models.yaml`` file.

    Attributes:
      name: Unique model identifier.
      desc: Human-readable description.
      filelist: Paths to the model's files.
      spec: Path to the block's specs.yaml, relative to models.yaml.
      axi_bundles: Path to the ``axi-bundles.yaml`` manifest, relative to models.yaml; used by ``rb axi-profile``.
      axi_monitor_out: Path where ``rb axi-profile gen-monitor`` writes the SystemVerilog monitor, relative to models.yaml.
      cdc: Path to the cdc.yaml for this model, relative to models.yaml. An optional ``#analysis_name`` fragment picks one entry. Read by ``rb hub``.
      synth: Path to the synth.yaml for this model, with the same fragment syntax. No tool reads it yet.
      tests: Path to the tests.yaml for this model, with the same fragment syntax. No tool reads it yet.
      graph: ``False`` opts a model with no elaborable root out of the ``rb graph build`` design tier; the config tier still emits its node.
      top: Root module of the filelist when it is not named after the model. Defaults to ``name`` and is the default top for cdc, synth, lint and fpga runs.
      path: Path to the models.yaml file, set by the loader.
    """

    name: str
    filelist: list[str]
    desc: str | None = None
    spec: str | None = None
    axi_bundles: str | None = None
    axi_monitor_out: str | None = None
    cdc: str | None = None
    synth: str | None = None
    tests: str | None = None
    graph: bool = True
    top: str | None = None
    path: str | None = None
    elaborations: list[ElaborationProfile] = field(default_factory=list)

    def _resolve_relative(self, rel: str) -> str:
        """Resolve ``rel`` against the models.yaml directory; absolute paths pass through.

        Without ``self.path`` (models built directly in tests) it falls back to the cwd.
        """
        if os.path.isabs(rel):
            return rel
        base = os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()
        return os.path.normpath(os.path.join(base, rel))

    def get_axi_bundles_path(self) -> str | None:
        """Absolute path to the model's ``axi-bundles.yaml``, or None. Does not check that the file exists."""
        if self.axi_bundles is None:
            return None
        return self._resolve_relative(self.axi_bundles)

    def get_axi_monitor_out_path(self) -> str | None:
        """Absolute path where ``gen-monitor`` writes the SV file, or None. The parent directory may not exist."""
        if self.axi_monitor_out is None:
            return None
        return self._resolve_relative(self.axi_monitor_out)

    def get_top(self) -> str:
        """The module this model's filelist is rooted at: ``top:`` if declared, else the model name."""
        return self.top or self.name

    def get_elaboration(self, profile_name: str) -> ElaborationProfile:
        for profile in self.elaborations:
            if profile.name == profile_name:
                return profile
        raise FatalRtlBuddyError(
            f"model {self.name!r} has no elaboration profile {profile_name!r}"
        )

    def get_model_name(self):
        """The model name."""
        return self.model_name

    def get_model_path(self):
        """The path to the models.yaml file."""
        return self.path

    def get_filelist(self):
        """The model's filelist paths."""
        return self.filelist

    def __str__(self):
        return pprint.pformat(self)


@serde
class ModelConfigFile:
    """A ``models.yaml`` file: its ``rtl-buddy-filetype`` (``model_config``) and model list."""

    rtl_buddy_filetype: Literal["model_config"] = field(rename="rtl-buddy-filetype")
    models: list[ModelConfig] = field(default_factory=list)


# TODO: Raise errors instead of killing things here
class ModelConfigLoader:
    """Loads and validates the models in one ``models.yaml``, reading the file once."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.models = []

        try:
            with open(self.path, "r") as file:
                data = from_yaml(ModelConfigFile, file.read())
                self.models = data.models
        except Exception as e:
            log_event(
                logger, logging.ERROR, "model_config.load_failed", path=path, error=e
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        seen: dict[str, int] = {}
        for idx, model in enumerate(self.models):
            model.path = self.path
            # Validate the name first: downstream code treats it as a path segment.
            validate_model_name(model.name, path)
            if model.top is not None:
                validate_model_top(model.top, model.name, path)
            if model.name in seen:
                log_event(
                    logger,
                    logging.ERROR,
                    "model_config.duplicate_model",
                    path=path,
                    name=model.name,
                    first_index=seen[model.name],
                    second_index=idx,
                )
                raise FatalRtlBuddyError(f"{path}: duplicate model name {model.name!r}")
            seen[model.name] = idx
            seen_profiles: dict[str, int] = {}
            for profile_idx, profile in enumerate(model.elaborations):
                _validate_elaboration_profile(profile, model, path)
                profile_key = profile.name.casefold()
                if profile_key in seen_profiles:
                    raise FatalRtlBuddyError(
                        f"{path}: model {model.name!r} has duplicate elaboration "
                        f"profile {profile.name!r}"
                    )
                seen_profiles[profile_key] = profile_idx

    def get_model(self, model_name: str) -> ModelConfig:
        """The :class:`ModelConfig` named ``model_name``; raises ``FatalRtlBuddyError`` if absent."""
        for model in self.models:
            if model.name == model_name:
                return model

        log_event(
            logger,
            logging.ERROR,
            "model_config.model_not_found",
            model=model_name,
            path=self.path,
        )
        raise FatalRtlBuddyError(f"model '{model_name}' not found")

    def get_models(self) -> list[ModelConfig]:
        return list(self.models)
