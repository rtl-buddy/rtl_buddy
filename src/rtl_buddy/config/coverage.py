import pprint

from dataclasses import dataclass
from serde import serde, field


@dataclass
class CoverageConfig:
    """Coverage post-processing settings for one simulator family (`name`, `use_lcov`)."""

    name: str
    use_lcov: bool

    def get_name(self) -> str:
        return self.name

    def get_use_lcov(self) -> bool:
        return self.use_lcov

    def __str__(self):
        return pprint.pformat(self)


@serde
class CoverageConfigFile:
    """YAML entry for `CoverageConfig`."""

    name: str
    use_lcov: bool = field(rename="use-lcov", default=False)

    def initialise(self) -> CoverageConfig:
        return CoverageConfig(
            name=self.name,
            use_lcov=self.use_lcov,
        )
