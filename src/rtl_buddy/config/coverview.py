import pprint
from collections.abc import Mapping

from dataclasses import dataclass
from serde import serde, field


@dataclass
class CoverviewConfig:
    """Coverview packaging settings for one simulator family.

    ``config`` holds inline Coverview JSON values; ``generate_tables`` names the coverage type used for tables.
    """

    name: str
    config: dict
    generate_tables: str | None

    def get_name(self) -> str:
        return self.name

    def get_config(self) -> dict:
        return self.config

    def get_generate_tables(self) -> str | None:
        return self.generate_tables

    def __str__(self):
        return pprint.pformat(self)


@serde
class CoverviewConfigFile:
    """YAML entry for `CoverviewConfig`."""

    name: str
    config: dict = field(default_factory=dict)
    generate_tables: str | None = field(rename="generate-tables", default=None)

    def initialise(self, root_cfg_path: str) -> CoverviewConfig:
        del root_cfg_path
        if not isinstance(self.config, Mapping):
            raise ValueError(
                f"cfg-coverview '{self.name}' config must be a mapping of inline Coverview JSON values"
            )
        return CoverviewConfig(
            name=self.name,
            config=dict(self.config),
            generate_tables=self.generate_tables,
        )
