import pprint

from dataclasses import dataclass
from serde import serde, field

from ..errors import FatalRtlBuddyError


@dataclass
class CoverageConfig:
    """Coverage post-processing settings for one simulator family (`name`, `use_lcov`, `merge_timeout`)."""

    name: str
    use_lcov: bool
    # Seconds the raw merge may run before it is stopped and reported as failed; None is no limit.
    merge_timeout: float | None = None

    def get_name(self) -> str:
        return self.name

    def get_use_lcov(self) -> bool:
        return self.use_lcov

    def get_merge_timeout(self) -> float | None:
        return self.merge_timeout

    def __str__(self):
        return pprint.pformat(self)


@serde
class CoverageConfigFile:
    """YAML entry for `CoverageConfig`."""

    name: str
    use_lcov: bool = field(rename="use-lcov", default=False)
    # No default limit: a legitimate merge of a few hundred databases can take minutes.
    merge_timeout: float | None = field(rename="merge-timeout", default=None)

    def initialise(self) -> CoverageConfig:
        if self.merge_timeout is not None and self.merge_timeout <= 0:
            raise FatalRtlBuddyError(
                f"cfg-coverage {self.name}: merge-timeout must be a number of "
                f"seconds greater than zero (got {self.merge_timeout!r}); omit it "
                "for no limit."
            )
        return CoverageConfig(
            name=self.name,
            use_lcov=self.use_lcov,
            merge_timeout=self.merge_timeout,
        )
