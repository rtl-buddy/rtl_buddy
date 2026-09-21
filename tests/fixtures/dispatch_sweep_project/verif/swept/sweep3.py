# Sweep hook that expands `wide` into three variants (an array > 1) and
# records every execution of this hook to $RB_SWEEP_COUNTER. Deterministic
# (no RNG) so the head's expansion and any accidental re-expansion would
# be identical — the test asserts the hook is invoked exactly once (on the
# head), so the counter file must contain a single line.
import copy
import os

_counter = os.environ.get("RB_SWEEP_COUNTER")
if _counter:
    with open(_counter, "a") as _f:
        _f.write("%d\n" % os.getpid())

out_test_cfgs = []
for _i in range(3):
    _cfg = copy.deepcopy(test_cfg)  # noqa: F821 (injected by exec_hook_script)
    _cfg.name = _cfg.name + ("_v%d" % _i)
    _cfg.set_plusarg("VARIANT", _i)
    out_test_cfgs.append(_cfg)
