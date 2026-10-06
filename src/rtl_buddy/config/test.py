import copy
import logging
from dataclasses import dataclass
from typing import Any, Literal
from serde import serde, field, from_dict, to_dict
from .dispatch import (
    DispatchResourcesFile,
    TestbenchCompileFile,
    validate_modes_block,
    validate_testbench_compile_block,
)
from .model import ModelConfig, ModelConfigLoader
from .uvm import UVMConfig

import pprint
import os

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..seeding import (
    SeedResolution,
    derive_test_seed,
    expanded_test_seed_identity,
    validate_sim_seed,
    validate_resolved_seed,
)

logger = logging.getLogger(__name__)


@serde
class CocotbTestbenchConfig:
    """cocotb settings for a testbench; `module` is the Python test module(s) passed via the MODULE env var."""

    module: str | list[str]

    def get_modules(self) -> list[str]:
        if isinstance(self.module, str):
            return [self.module]
        return list(self.module)


@serde
class SystemCTestbenchConfig:
    """SystemC settings for a testbench; presence makes Verilator emit the DUT as an sc_module linked against a user sc_main().

    `sc_main` is the C++ file containing sc_main(), relative to the suite directory. `sc_extra` lists extra C++ units. `cflags` and `ldflags` are appended to -CFLAGS and -LDFLAGS. `pin_style` "uint" or "biguint" adds --pins-sc-uint or --pins-sc-biguint; "bv" adds no flag; None keeps Verilator's default.
    """

    sc_main: str
    sc_extra: list[str] = field(default_factory=list)
    cflags: list[str] = field(default_factory=list)
    ldflags: list[str] = field(default_factory=list)
    pin_style: Literal["uint", "bv", "biguint"] | None = None


@serde
class TestbenchConfig:
    """A testbench entry in a suite.

    `filelist` lists the files to compile. `toplevel` is the module the compile elaborates from (Verilator ``--top-module``, VCS ``-top``, Icarus ``-s``): required for cocotb and SystemC, recommended otherwise. For plain SystemVerilog it is the bench, not the DUT. `cocotb` or `systemc` presence selects that mode; they are mutually exclusive. `resources` is the default dispatch reservation for the testbench's tests, with a ``modes:`` sub-block per builder mode. `compile` is this testbench's compile reservation, layered over the suite's ``compile:`` block and aggregated by :func:`~.dispatch.aggregate_compile_resources`; ``parallel`` is rejected here.
    """

    name: str
    filelist: list[str]
    toplevel: str | None = None
    cocotb: CocotbTestbenchConfig | None = None
    systemc: SystemCTestbenchConfig | None = None
    resources: DispatchResourcesFile | None = None
    compile: TestbenchCompileFile | None = None

    def __post_init__(self):
        # Validated at load, as the suite-level block is: an unquoted `4:00:00` parses as an integer.
        try:
            self.compile = validate_testbench_compile_block(self.compile)
            # Only `modes:` is validated here; resolve_resources validates the base fields when applied.
            if self.resources is not None:
                self.resources.modes = validate_modes_block(
                    self.resources.modes, where="resources."
                )
        except FatalRtlBuddyError as e:
            raise FatalRtlBuddyError(f"testbench '{self.name}': {e}") from e
        if self.cocotb is not None and self.systemc is not None:
            raise FatalRtlBuddyError(
                f"testbench '{self.name}': cocotb: and systemc: are mutually exclusive "
                "(different host kernels cannot share one Verilator build)"
            )
        if self.cocotb is not None and self.toplevel is None:
            raise FatalRtlBuddyError(
                f"testbench '{self.name}': toplevel is required when cocotb: is present"
            )
        if self.systemc is not None and self.toplevel is None:
            raise FatalRtlBuddyError(
                f"testbench '{self.name}': toplevel is required when systemc: is present"
            )

    def is_cocotb(self) -> bool:
        return self.cocotb is not None

    def is_systemc(self) -> bool:
        return self.systemc is not None

    def get_name(self):
        return self.name

    def get_filelist(self):
        return self.filelist

    def __str__(self):
        return pprint.pformat(self)


@dataclass
class TestConfig:
    """A test loaded from tests.yaml. Hooks and plan restore may mutate it during a run.

    `_reglvl` is an int, a per-builder dict or None (0). `pa` and `pd` are the plusargs and plusdefines. `uvm` enables UVM report parsing in post. `sweep_path`, `preproc_path` and `postproc_path` are the hook scripts. `assertions` compiles SVA with Verilator `--assert` and `--coverage-user` and reports assertion-failed counts.
    """

    name: str
    desc: str
    model: ModelConfig
    _reglvl: int | dict | None
    pa: dict | None
    pd: dict | None
    uvm: UVMConfig | None
    preproc_path: str | None
    postproc_path: str | None
    sweep_path: str | None
    tb: TestbenchConfig
    timeout: int | None
    covers: list[str] | None = None
    builder_name: str | None = None
    assertions: bool = False
    # Layered over the testbench's `resources:` and cfg-dispatch defaults by config.dispatch.resolve_resources().
    resources: "DispatchResourcesFile | None" = None
    # Either flag makes a FAIL report as XFAIL (a pass). An unexpected pass (XPASS) counts as a pass under `xfail` and a failure under `xfail_strict`, which wins if both are set.
    xfail: bool = False
    xfail_strict: bool = False
    default_timeout: int = 60
    sim_rand_seed: int | None = None
    sim_rand_seed_plusarg: str | None = None
    # False declares that the `preproc` hook leaves the compile key alone, so the dispatch head plans this test's build with hook-less tests on the same key.
    preproc_sets_plusdefines: bool = True
    resolved_seed: int | None = None
    seed_source: str | None = None
    seed_identity: str | None = None

    def get_name(self):
        return self.name

    def get_builder_name(self):
        """Return the ``cfg-rtl-builder`` name selected for this test, or None for the suite/platform default."""
        return self.builder_name

    def is_xfail(self) -> bool:
        """Whether this test is expected to fail."""
        return self.xfail or self.xfail_strict

    def get_xfail(self) -> bool:
        """Whether `xfail` is set."""
        return self.xfail

    def get_xfail_strict(self) -> bool:
        """Whether an unexpected pass (XPASS) should count as a failure."""
        return self.xfail_strict

    def get_model(self):
        return self.model

    def get_testbench(self):
        return self.tb

    def get_plusarg(self, key):
        """Return the value of plusarg `key`."""
        return self.pa.get(key)

    def get_plusargs(self):
        """Return the plusargs dict, or None."""
        return self.pa

    def set_plusarg(self, key, value):
        """Set one plusarg; used by preprocessing hooks."""
        if self.pa is None:
            self.pa = {}
        self.pa[key] = value

    def set_plusargs(self, new_args):
        """Merge `new_args` into the plusargs; used by preprocessing hooks."""
        if self.pa is None:
            self.pa = {}
        self.pa.update(new_args)

    def with_plusarg_overrides(self, overrides):
        """Return this config with ``rb test --plusarg`` ``overrides`` merged over its ``plusargs:``.

        Overrides win over configured values; a ``preproc`` hook may still overwrite them. Empty overrides return ``self``; otherwise a shallow copy with a fresh ``plusargs`` dict. A ``None`` value is a valueless ``+KEY``.

        Raises FatalRtlBuddyError if an override names the test's ``sim-rand-seed-plusarg``.
        """
        if not overrides:
            return self
        if (
            self.sim_rand_seed_plusarg is not None
            and self.sim_rand_seed_plusarg in overrides
        ):
            # ensure_resolved_seed_plusarg rewrites this plusarg after the preprocessor, so an override would be lost.
            raise FatalRtlBuddyError(
                f"test {self.name!r}: --plusarg {self.sim_rand_seed_plusarg} "
                "names the plusarg its sim-rand-seed-plusarg manages, and "
                "rtl_buddy restores the resolved seed there — the override "
                "would be dropped. Choose the seed with --master-seed or a "
                "test-level sim-rand-seed instead."
            )
        merged = copy.copy(self)
        merged.pa = {**(self.pa or {}), **overrides}
        return merged

    def resolve_runtime_seed(
        self, *, master_seed: int | None, suite_identity: str, run_id: int | None
    ) -> SeedResolution | None:
        """Resolve and expose this run's seed before its preprocessor executes."""
        if self.sim_rand_seed is not None:
            resolution = SeedResolution(
                seed=validate_sim_seed(self.sim_rand_seed),
                source="fixed",
                identity=expanded_test_seed_identity(suite_identity, self.name, run_id),
            )
        elif master_seed is not None:
            resolution = derive_test_seed(
                master_seed,
                suite_identity=suite_identity,
                test_name=self.name,
                run_id=run_id,
            )
        else:
            return None

        self.set_resolved_seed(resolution)
        return resolution

    def set_resolved_seed(self, resolution: SeedResolution) -> None:
        """Store a planned seed and update its configured runtime plusarg."""
        self.resolved_seed = validate_resolved_seed(resolution.seed, resolution.source)
        self._resolved_seed_lock = self.resolved_seed
        self.seed_source = resolution.source
        self.seed_identity = resolution.identity
        self.ensure_resolved_seed_plusarg()

    def ensure_resolved_seed_plusarg(self) -> None:
        """Restore the managed seed plusarg after a preprocessor mutation."""
        resolved_seed = self.get_resolved_seed()
        if resolved_seed is not None and self.sim_rand_seed_plusarg is not None:
            self.set_plusarg(self.sim_rand_seed_plusarg, resolved_seed)

    def get_resolved_seed(self) -> int | None:
        """Return the pre-resolved runtime seed, when this run has one."""
        return getattr(self, "_resolved_seed_lock", self.resolved_seed)

    def get_plusdefine(self, key):
        """Return the value of plusdefine `key`."""
        return self.pd.get(key)

    def get_plusdefines(self):
        """Return the plusdefines dict, or None."""
        return self.pd

    def set_plusdefine(self, key, value):
        """Set one plusdefine; used by preprocessing hooks."""
        if self.pd is None:
            self.pd = {}
        self.pd[key] = value

    def set_plusdefines(self, new_defines):
        """Merge `new_defines` into the plusdefines; used by preprocessing hooks."""
        if self.pd is None:
            self.pd = {}
        self.pd.update(new_defines)

    def get_timeout(self):
        """Return ``(timeout_seconds, is_custom)``; `is_custom` is False when the default applies."""
        is_custom = self.timeout is not None
        return self.timeout if is_custom else self.default_timeout, is_custom

    def set_timeout(self, timeout):
        """Set the simulation timeout in seconds."""
        self.timeout = timeout

    def get_sweep_path(self):
        """Return the absolute path of the sweep script (expands one test into variants), or None."""
        return self.sweep_path

    def get_preproc_path(self):
        """Return the absolute path of the preprocessing script (runs before compile), or None."""
        return self.preproc_path

    def get_postproc_path(self):
        """Return the absolute path of the postprocessing script (runs after simulation), or None."""
        return self.postproc_path

    def get_reglvl(self, builder):
        """Return the regression level for `builder`.

        Order: the builder's entry in a ``reglvl`` dict, its ``default`` entry, an int ``reglvl``, else 0. Raises FatalRtlBuddyError if a dict has neither.
        """
        match self._reglvl:
            case int() as lvl:
                reglvl = lvl
            case dict() if builder in self._reglvl:
                reglvl = self._reglvl[builder]
            case dict() if "default" in self._reglvl:
                reglvl = self._reglvl["default"]
            case None:
                reglvl = 0
            case _:
                log_event(
                    logger,
                    logging.ERROR,
                    "test_config.reglvl_malformed",
                    test=self.name,
                    builder=builder,
                )
                raise FatalRtlBuddyError(
                    f"Malformed tests.yaml, specify reglvl for {self.name} with {builder} or default"
                )

        return reglvl

    # Dispatch plan round trip: the head expands sweeps once and writes each TestConfig to the plan manifest; jobs rebuild from it. Every field is carried.

    # to_plan_dict keys that differ from their dataclass field.
    _PLAN_FIELD_RENAMES = {"_reglvl": "reglvl"}

    def to_plan_dict(self) -> dict:
        """Serialize to a JSON-safe dict for the dispatch plan manifest."""
        return {
            "name": self.name,
            "desc": self.desc,
            "model": to_dict(self.model),
            "reglvl": self._reglvl,
            "pa": self.pa,
            "pd": self.pd,
            "uvm": to_dict(self.uvm) if self.uvm is not None else None,
            "preproc_path": self.preproc_path,
            "postproc_path": self.postproc_path,
            "sweep_path": self.sweep_path,
            "tb": to_dict(self.tb),
            "timeout": self.timeout,
            "covers": self.covers,
            "builder_name": self.builder_name,
            "assertions": self.assertions,
            "resources": to_dict(self.resources)
            if self.resources is not None
            else None,
            "xfail": self.xfail,
            "xfail_strict": self.xfail_strict,
            "default_timeout": self.default_timeout,
            "sim_rand_seed": self.sim_rand_seed,
            "sim_rand_seed_plusarg": self.sim_rand_seed_plusarg,
            "preproc_sets_plusdefines": self.preproc_sets_plusdefines,
            "resolved_seed": self.get_resolved_seed(),
            "seed_source": self.seed_source,
            "seed_identity": self.seed_identity,
        }

    @classmethod
    def from_plan_dict(cls, d: dict) -> "TestConfig":
        """Rebuild a TestConfig from a :meth:`to_plan_dict` manifest entry; paths are already absolute."""
        config = cls(
            d["name"],
            d["desc"],
            from_dict(ModelConfig, d["model"]),
            d["reglvl"],
            d["pa"],
            d["pd"],
            from_dict(UVMConfig, d["uvm"]) if d["uvm"] is not None else None,
            d["preproc_path"],
            d["postproc_path"],
            d["sweep_path"],
            from_dict(TestbenchConfig, d["tb"]),
            d["timeout"],
            covers=d["covers"],
            builder_name=d["builder_name"],
            assertions=d["assertions"],
            resources=from_dict(DispatchResourcesFile, d["resources"])
            if d["resources"] is not None
            else None,
            xfail=d["xfail"],
            xfail_strict=d["xfail_strict"],
            default_timeout=d["default_timeout"],
            sim_rand_seed=d.get("sim_rand_seed"),
            sim_rand_seed_plusarg=d.get("sim_rand_seed_plusarg"),
            preproc_sets_plusdefines=d.get("preproc_sets_plusdefines", True),
            resolved_seed=d.get("resolved_seed"),
            seed_source=d.get("seed_source"),
            seed_identity=d.get("seed_identity"),
        )

        if config.resolved_seed is not None:
            try:
                config.set_resolved_seed(
                    SeedResolution(
                        config.resolved_seed, config.seed_source, config.seed_identity
                    )
                )
            except ValueError as e:
                raise FatalRtlBuddyError(
                    f"dispatch plan seed for {config.name!r} is invalid: {e}"
                ) from e
        return config

    def __str__(self):
        return pprint.pformat(self)


@serde
class TestConfigFile:
    name: str
    desc: str
    model: str
    model_path: str
    _reglvl: int | dict | None = field(rename="reglvl")
    pa: dict | None = field(rename="plusargs")
    pd: dict | None = field(rename="plusdefines")
    uvm: UVMConfig | None
    preproc_path: str | None = field(
        rename="preproc",
        deserializer=lambda data: data.get("path") if data is not None else None,
    )
    postproc_path: str | None = field(
        rename="postproc",
        deserializer=lambda data: data.get("path") if data is not None else None,
    )
    sweep_path: str | None = field(
        rename="sweep",
        deserializer=lambda data: data.get("path") if data is not None else None,
    )
    tb: str = field(rename="testbench")
    timeout: int | None = field(rename="sim_timeout")
    covers: list[str] | None = None
    builder_name: str | None = field(rename="builder", default=None)
    assertions: bool = False
    xfail: bool = False
    xfail_strict: bool = False
    resources: DispatchResourcesFile | None = None
    sim_rand_seed: int | None = field(rename="sim-rand-seed", default=None)
    sim_rand_seed_plusarg: str | None = field(
        rename="sim-rand-seed-plusarg", default=None
    )
    # Any, not bool: validated in initialise() so a wrong value names the key.
    preproc_sets_plusdefines: Any = field(
        rename="preproc-sets-plusdefines", default=None
    )

    def initialise(
        self,
        config_dir,
        tbs,
        suite_builder=None,
        suite_preproc_sets_plusdefines=None,
    ):
        validate_preproc_sets_plusdefines(
            self.preproc_sets_plusdefines, where=f"test {self.name!r}"
        )
        if self.sim_rand_seed is not None:
            try:
                validate_sim_seed(self.sim_rand_seed)
            except ValueError as e:
                raise FatalRtlBuddyError(f"test {self.name!r}: {e}") from e
        if self.sim_rand_seed_plusarg == "":
            raise FatalRtlBuddyError(
                f"test {self.name!r}: sim-rand-seed-plusarg must not be empty"
            )
        if self.resources is not None:
            try:
                self.resources.modes = validate_modes_block(
                    self.resources.modes, where="resources."
                )
            except FatalRtlBuddyError as e:
                raise FatalRtlBuddyError(f"test {self.name!r}: {e}") from e
        tb = tbs[self.tb]
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        # Hook paths are relative to the suite config; resolve them so they open from any cwd.
        preproc_path = _resolve_hook_path(self.preproc_path, config_dir)
        postproc_path = _resolve_hook_path(self.postproc_path, config_dir)
        sweep_path = _resolve_hook_path(self.sweep_path, config_dir)
        return TestConfig(
            self.name,
            self.desc,
            model,
            self._reglvl,
            self.pa,
            self.pd,
            self.uvm,
            preproc_path,
            postproc_path,
            sweep_path,
            tb,
            self.timeout,
            covers=self.covers,
            builder_name=self.builder_name or suite_builder,
            assertions=self.assertions,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
            resources=self.resources,
            sim_rand_seed=self.sim_rand_seed,
            sim_rand_seed_plusarg=self.sim_rand_seed_plusarg,
            # Test value, else the suite's, else the conservative default.
            preproc_sets_plusdefines=next(
                (
                    value
                    for value in (
                        self.preproc_sets_plusdefines,
                        suite_preproc_sets_plusdefines,
                    )
                    if value is not None
                ),
                True,
            ),
        )


def validate_preproc_sets_plusdefines(value, *, where):
    """Reject a ``preproc-sets-plusdefines`` that is neither unset nor a boolean.

    Raises FatalRtlBuddyError naming ``where``; a quoted ``"false"`` would otherwise read as true.
    """
    if value is not None and not isinstance(value, bool):
        raise FatalRtlBuddyError(
            f"{where}: preproc-sets-plusdefines must be true or false, got {value!r}"
        )


def parse_plusarg_overrides(values) -> dict:
    """Parse repeated ``--plusarg`` values into ``{key: value}``.

    Each value is ``KEY=VALUE``, or a bare ``KEY`` for a valueless plusarg (``None``). A repeated key keeps its last value. Raises FatalRtlBuddyError for an empty key, a ``+`` in the key, or whitespace in the key.
    """
    overrides: dict = {}
    for raw in values or []:
        key, sep, value = raw.partition("=")
        # strip("+") so "+" or "++" reports a missing name, not the '+' error.
        if not key.strip("+"):
            raise FatalRtlBuddyError(
                f"--plusarg {raw!r} has no name; write --plusarg KEY=VALUE "
                "(or --plusarg KEY for a valueless plusarg)"
            )
        if "+" in key:
            raise FatalRtlBuddyError(
                f"--plusarg {raw!r}: the name must not contain '+' — rtl_buddy "
                f"adds it, so write --plusarg {key.lstrip('+')}"
                f"{'=' + value if sep else ''}"
            )
        if any(c.isspace() for c in key):
            raise FatalRtlBuddyError(
                f"--plusarg {raw!r}: the name must not contain whitespace; a "
                "space would split it into a second plusarg on the simulator "
                "command line"
            )
        overrides[key] = value if sep else None
    return overrides


def _resolve_hook_path(path: str | None, config_dir: str) -> str | None:
    """Resolve a hook path from tests.yaml: absolute paths pass through, relative ones anchor on `config_dir`, None stays None."""
    if path is None:
        return None
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(config_dir, path))
