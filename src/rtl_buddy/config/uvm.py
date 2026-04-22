from serde import serde, field


@serde
class UVMConfig:
    """
    Configuration for UVM report parsing in post.

    Attributes:
            max_warns (int): Maximum number of UVM_WARNINGs before the test fails. Defaults to 0.
            max_errors (int): Maximum number of UVM_ERRORs before the test fails. Defaults to 0.
    """

    max_warns: int = field(default=0)
    max_errors: int = field(default=0)

    def __post_init__(self):
        if self.max_warns < 0:
            raise ValueError
        if self.max_errors < 0:
            raise ValueError
