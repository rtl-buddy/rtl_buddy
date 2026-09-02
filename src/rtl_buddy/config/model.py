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

#: A model ``name:`` has to be safe as a **single path segment**, because
#: that is what it becomes: ``artefacts/hier/<name>/``,
#: ``artefacts/graph/design/<name>/``, and the per-model directories every
#: flow writes. Nothing downstream re-checks it, and ``rb graph build``
#: *deletes* ``design/<name>/`` when a model opts out — so a name like
#: ``..`` or ``/tmp`` would escape the artefact tree with the caller's
#: permissions. It is also the model's default top module, so an
#: identifier-shaped name is what every project already writes.
#:
#: Deliberately a little wider than a SystemVerilog identifier: ``-`` and
#: ``.`` inside the name are harmless as a path segment and plausible in
#: an existing project. The leading character may not be ``.``, which is
#: what rules out ``.`` and ``..``; ``/`` and ``\\`` are absent from the
#: class entirely, which rules out every separator and absolute path.
#: Anchored with ``\\Z``, not ``$``: Python's ``$`` also matches before a
#: trailing newline, so ``"blk_a\\n"`` would otherwise pass a rule whose
#: whole purpose is that the value carries no newline.
MODEL_NAME_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def validate_model_name(name: str, path: str) -> None:
    """Raise unless ``name`` is safe as an artefact directory name.

    Args:
      name: the ``name:`` field as written.
      path: the models.yaml it came from, for the message.

    Raises:
      FatalRtlBuddyError: naming the file, the value and the rule.
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


#: A model ``top:`` must be a **simple** SystemVerilog identifier. It is
#: the module name every backend elaborates from, and it does not stay in
#: HDL: the FPGA flows join it into artefact paths (``<top>.bit``), and
#: the Yosys, Vivado and OpenROAD generators interpolate it into Tcl
#: (``set top <top>``, ``synth_design -top <top>``). None of those quote
#: it, so a value carrying a path separator, a newline or a shell/Tcl
#: metacharacter would write outside the artefact directory or append
#: commands to a generated script. This rule is what makes the
#: downstream interpolation safe, and it is enforced once, here, rather
#: than escaped differently in each flow.
#:
#: ``$`` is legal in a SystemVerilog identifier and is **excluded here
#: anyway**, because it is a substitution character in exactly the Tcl
#: this value is interpolated into unquoted: ``synth_design -top foo$bar``
#: makes Vivado substitute an empty (or wrong) ``$bar`` and elaborate a
#: different module than the YAML names, or fail outright. Having chosen
#: to make the value safe at the boundary rather than escape it in six
#: generators, the rule has to be the intersection of "legal SV" and
#: "inert in Tcl and in a filename" — not the union. A design whose top
#: really is named with a ``$`` has to be renamed or wrapped.
#:
#: SystemVerilog also has *escaped* identifiers — a backslash, then
#: printable characters, then whitespace — which legally admit ``/`` and
#: ``;``. Those are refused outright for the same reason: no flow can
#: name a file or a Tcl token after one safely, and a design that needs
#: one cannot be driven through these flows anyway.
#:
#: ``get_top()`` falls back to the model ``name`` when ``top:`` is unset,
#: so :data:`MODEL_NAME_RE` reaches the same Tcl. It is wider — it allows
#: ``-`` and ``.`` — but neither is a Tcl metacharacter, and its first
#: character may not be ``-``, so a name can never be read as an option
#: flag either. The safety invariant holds on both paths.
#:
#: Anchored with ``\\Z`` for the reason :data:`MODEL_NAME_RE` states.
MODEL_TOP_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
ELAB_TIMESCALE_RE = re.compile(r"\A\d+(?:s|ms|us|ns|ps|fs)/\d+(?:s|ms|us|ns|ps|fs)\Z")
ELAB_WARNING_RE = re.compile(
    r"\A(?:none|all|error|no-[A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"error=[A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"no-error=[A-Za-z0-9_][A-Za-z0-9_-]*|"
    r"[A-Za-z0-9_][A-Za-z0-9_-]*)\Z"
)
ELAB_BASE_ARTIFACT_NAME = "base"


def validate_top(
    top: str,
    name: str,
    path: str,
    *,
    subject: str = "model",
    event: str = "model_config.invalid_model_top",
) -> None:
    """Raise unless ``top`` is a simple SystemVerilog identifier.

    ``fpv.yaml`` and ``mut.yaml`` carry their own ``top:`` which wins over
    the model's and reaches the same generators, so they validate against
    this rule too — under their own event name and wording.

    Args:
      top: the ``top:`` field as written.
      name: the model / verification / campaign declaring it, for the message.
      path: the YAML it came from, for the message.
      subject: what ``name`` names, for the message.
      event: the machine event to log.

    Raises:
      FatalRtlBuddyError: naming the file, the declarer, the value and the rule.
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
    """Raise unless a models.yaml ``top:`` is a simple SV identifier."""
    validate_top(top, name, path)


def split_back_pointer(value: str) -> tuple[str, str | None]:
    """Split a ``cdc:``/``synth:``/``tests:`` back-pointer into
    ``(path, entry_name | None)``.

    The path side is the relative location of the downstream YAML
    (resolved by the caller against the parent ``models.yaml``).
    The optional ``#entry_name`` fragment names a single analysis /
    synthesis / test inside that file — useful when one file holds
    multiple and the model wants to pin one as canonical.
    """
    if "#" in value:
        path, _, entry = value.partition("#")
        entry = entry.strip()
        return path, (entry if entry else None)
    return value, None


def resolve_back_pointer(
    model: "ModelConfig", field_name: str
) -> tuple[str, str | None] | None:
    """Resolve ``model.<field_name>`` (one of ``cdc``/``synth``/``tests``)
    into an absolute ``(path, entry_name | None)`` tuple.

    Returns ``None`` when the field is unset on the model. Raises
    ``FatalRtlBuddyError`` when the field is set but ``model.path``
    is missing (loader didn't tag the model — programming error).
    Delegates path resolution to ``ModelConfig._resolve_relative`` so
    the cdc/synth/tests fields share semantics with the existing
    ``axi_bundles`` / ``axi_monitor_out`` resolution.
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
    if profile.name == ELAB_BASE_ARTIFACT_NAME:
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
    profile.resources = validate_resources_block(profile.resources)
    if profile.resources is not None and profile.resources.cpus is not None:
        cpus = profile.resources.cpus
        if not isinstance(cpus, int) or isinstance(cpus, bool) or cpus < 1:
            raise FatalRtlBuddyError(
                f"{prefix} resources.cpus must be a positive integer"
            )


@serde
class ModelConfig:
    """
    Representation of a single model entry in a 'model_config' file

    Attributes
      name (str): Unique model identifier.
      desc (str|None): Human-readable model description.
      filelist (list[str]): List of paths to files associated with the model.
      spec (str|None): Relative path from models.yaml to the block's specs.yaml.
      axi_bundles (str|None): Relative path from models.yaml to the
        block's ``axi-bundles.yaml`` manifest, when AXI profiling is
        configured for this model. Consumed by ``rb axi-profile``.
      axi_monitor_out (str|None): Relative path from models.yaml to
        where ``rb axi-profile gen-monitor`` should write the generated
        SystemVerilog monitor file. Typically points into the verif
        testbench source tree so the file is picked up by the tb's
        filelist (e.g. ``../verif/soc_top/gen/axi_perf_mon.sv``).
      cdc (str|None): Relative path from models.yaml to the cdc.yaml that owns
        this model's CDC analysis. Optional ``#analysis_name`` fragment picks one
        entry from a multi-analysis file (e.g. ``cdc.yaml#full_design``). Read by
        ``rb hub`` to wire up the clock-domain overlay; absent → overlay
        unavailable.
      synth (str|None): Relative path from models.yaml to the synth.yaml that
        owns this model's synthesis flow. Same ``#synth_name`` fragment semantics.
        Not consumed by any tool yet — declared now so the schema doesn't churn
        when future hub overlays (e.g. synthesis QoR) want to look it up.
      tests (str|None): Relative path from models.yaml to the tests.yaml that
        owns this model's testbench/test suite. Same ``#test_name`` fragment
        semantics. Not consumed by any tool yet.
      graph (bool): Whether this model takes part in ``rb graph build``'s
        design tier. ``false`` opts it out for the models that have no
        elaborable root at all — an SV ``interface`` published as a library
        entry, or a filelist of vendored IP with no module named after the
        model. The config tier still emits the model node (so spec and
        test cross-references resolve); the design tier records the model
        as *skipped* rather than attempting an export that can only fail.
      top (str|None): Root module of this model's filelist, when it is not
        named after the model. Defaults to ``name``, which is the project
        convention every flow assumed before this field existed. Feeds
        ``get_top()``, so it is also the default top of a ``cdc.yaml`` /
        ``synth.yaml`` / ``lint.yaml`` / ``fpga.yaml`` run against this
        model — the same escape hatch ``fpv.yaml`` already spells per-run.
      path (str|None): Path to the model config file. Will usually be set by the loader.
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
        """Resolve ``rel`` against the directory containing models.yaml.

        Absolute paths pass through unchanged. The loader always sets
        ``self.path``; an unset path falls back to cwd (only reachable
        from tests that construct ``ModelConfig`` directly).
        """
        if os.path.isabs(rel):
            return rel
        base = os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()
        return os.path.normpath(os.path.join(base, rel))

    def get_axi_bundles_path(self) -> str | None:
        """Absolute path to the model's ``axi-bundles.yaml`` (or None).

        Does not check that the file exists — callers should error
        with a hint to run ``rb axi-profile discover`` when missing.
        """
        if self.axi_bundles is None:
            return None
        return self._resolve_relative(self.axi_bundles)

    def get_axi_monitor_out_path(self) -> str | None:
        """Absolute path where ``gen-monitor`` should write the SV file (or None).

        Parent directory may not exist yet at load time.
        """
        if self.axi_monitor_out is None:
            return None
        return self._resolve_relative(self.axi_monitor_out)

    def get_top(self) -> str:
        """The module this model's filelist is rooted at.

        ``top:`` when declared, else the model name — the convention
        ``rb hier``, ``rb graph build`` and the non-simulation flows all
        relied on implicitly before the override existed. Returned even
        for a ``graph: false`` model: the opt-out says the model is not
        worth elaborating, not that this fallback changed.
        """
        return self.top or self.name

    def get_elaboration(self, profile_name: str) -> ElaborationProfile:
        for profile in self.elaborations:
            if profile.name == profile_name:
                return profile
        raise FatalRtlBuddyError(
            f"model {self.name!r} has no elaboration profile {profile_name!r}"
        )

    def get_model_name(self):
        """
        Retrieve the value of model_name.

        Returns:
        model_name (str): The value of model_name in the model.
        """
        return self.model_name

    def get_model_path(self):
        """
        Retrieve the value of path.

        Returns:
        path (str): The value of path in the model. The path to the model config file.
        """
        return self.path

    def get_filelist(self):
        """
        Retrieve the value of filelist.

        Returns:
        filelist (list[str]): The value of filelist in the model.
        """
        return self.filelist

    def __str__(self):
        return pprint.pformat(self)


@serde
class ModelConfigFile:
    """
    Representation of a 'model_config' file.

    Attributes
      rtl_buddy_filetype (Literal['model_config']): Config file type. Must be 'model_config'.
      models (list[RawModelConfig]): List of model configurations.
    """

    rtl_buddy_filetype: Literal["model_config"] = field(rename="rtl-buddy-filetype")
    models: list[ModelConfig] = field(default_factory=list)


# TODO: Raise errors instead of killing things here
class ModelConfigLoader:
    """
    Helper class to load model configurations from a file. Reads the file once.

    Attributes:
      models(list[RawModelConfig]): List of raw model configs.
    """

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

        # Fail loud on duplicate ``name:`` — silently letting the
        # first or last win makes "model X not found" errors at
        # lookup time and hides the user's typo. Caught here so
        # every downstream consumer (rb cdc, rb synth, rb hier,
        # rb hub) sees a single source of truth.
        seen: dict[str, int] = {}
        for idx, model in enumerate(self.models):
            model.path = self.path
            # Before anything else: the name is a path segment everywhere
            # downstream, and one consumer deletes the directory it names.
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
                if profile.name in seen_profiles:
                    raise FatalRtlBuddyError(
                        f"{path}: model {model.name!r} has duplicate elaboration "
                        f"profile {profile.name!r}"
                    )
                seen_profiles[profile.name] = profile_idx

    def get_model(self, model_name: str) -> ModelConfig:
        """
        Get a ModelConfig according to model_name.

        Args:
          name (str): Unique system identifier for the model.
          model_name (str): Unique identifier for the model in file.
        Returns:
          model (ModelConfig): The model configuration.
        Raises:
          Panics if no model corresponding to model_name can be found.
        """
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
