from serde import serde, field


@serde
class UVMConfig:
    """UVM report limits: the test fails above `max_warns` warnings or `max_errors` errors (both default 0)."""

    max_warns: int = field(default=0)
    max_errors: int = field(default=0)

    def __post_init__(self):
        if self.max_warns < 0:
            raise ValueError
        if self.max_errors < 0:
            raise ValueError
