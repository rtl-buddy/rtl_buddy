import logging
import os

logger = logging.getLogger(__name__)
import pprint

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


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
    path: str | None = None

    def _resolve_relative(self, rel: str) -> str:
        """Resolve ``rel`` against the directory containing models.yaml.

        Absolute paths pass through unchanged. Relative paths are
        anchored at ``dirname(self.path)`` when ``self.path`` is set
        (the loader normally sets it); otherwise the cwd is used.
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
                model.path = self.path
                return model

        log_event(
            logger,
            logging.ERROR,
            "model_config.model_not_found",
            model=model_name,
            path=self.path,
        )
        raise FatalRtlBuddyError(f"model '{model_name}' not found")
