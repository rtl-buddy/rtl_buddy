## Gate conflicting drivers

When call sites are split across a combinational and a clocked process, the
shared net takes conflicting drivers and folds to `x`, taking its register and
everything downstream with it. Yosys reports this as a
`multiple conflicting drivers` warning and still exits 0.
`conflicting-drivers: error`, the default, turns those warnings into a failed
run naming the count and the log path. Set `allow` only when the warnings are
understood and accepted.

A legitimate multi-driver tristate bus produces the same warning, one per bit.
Those are not counted: a warning whose drivers are all `$tribuf` / `$_TBUF_`
cells and module ports is a working design, and only a warning with at least
one other driver — a flop, a process action — fails the run.

Both gates apply to the Yosys elaboration stage, which the `yosys` and
`openroad` backends share. An unrecognized value for either option is fatal.

### Upgrading

These gates are new, and `static-functions` defaults to `error` under
`frontend: slang`. A slang synthesis run that passed before can now **fail**,
including one whose subroutines have a single call site and whose netlist
happens to be correct — the scan reports the declaration, not the aliasing.
That default is deliberate: the failure mode it guards is a silently corrupted
netlist with plausible area and timing numbers. To stage the migration, set
`static-functions: warn` while the declarations are fixed; each run then still
reports `static_function_findings` in its machine output.
