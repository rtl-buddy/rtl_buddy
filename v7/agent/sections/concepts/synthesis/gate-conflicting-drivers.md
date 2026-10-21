## Gate conflicting drivers

When one net is driven from both a combinational and a clocked process, for example through aliased static-function arguments, it folds to `x` and takes its register and everything downstream with it. Yosys reports only a `multiple conflicting drivers` warning and exits 0.

`conflicting-drivers: error` (default) fails the run and names the count and the log path. Set `allow` only when the warnings are understood.

Tristate-bus warnings (all drivers are tristate cells and ports) are not counted; any other driver, such as a flop, fails the run.
