## Prerequisites

`cocotb` must be installed in the active Python environment:

```bash
uv add cocotb
# or: pip install cocotb
```

The runner calls `cocotb-config` at compile time. If it is missing, `rtl_buddy` raises a `FatalRtlBuddyError` with an actionable message rather than a raw traceback.
