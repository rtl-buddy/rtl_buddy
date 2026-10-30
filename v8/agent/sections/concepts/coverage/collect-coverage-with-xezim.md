## Collect coverage with xezim

A xezim builder collects coverage when a `compile-time` option starts with `--code-coverage` or `+cover`. SVA `assertions:` alone does not turn it on. Add a `cfg-coverage` entry named `xezim`:

```yaml
cfg-rtl-builder:
  - name: xezim
    builder: xezim
    builder-simv: simv
    builder-opts:
      cov:
        compile-time: --code-coverage=all
        run-time: ""

cfg-coverage:
  - name: xezim
    use-lcov: true
```

Each run writes `artefacts/<test>/coverage.json` (`run-NNNN/coverage.json` for `randtest`) through `XEZIM_COV_DB`. The merge flags work as for Verilator. `--coverage-merge` sums the per-test files in Python into `cov_dir/coverage_merged.json`; there is no xezim merge tool, and `merge-timeout` bounds the merge. The points read differently from Verilator's:

| Metric | xezim points |
|---|---|
| Line | One point per statement, so a line with two statements has two points. `column` numbers the statements on a line from 0. |
| Branch | One point per arm, on the arm's line, named by the arm (`if`, `else`, a case item). |
| Toggle | One point per bit, named `sig[i]`. A bit is hit only when it rose and fell. xezim counts each direction separately, so its own toggle percentage is higher. Toggles have no source line: they are filed under the module's file at line 0. |
| Cover | One point per `cover` statement, named by its label (`label: cover property ...`): from the `name` field xezim writes since 0.11.3 (git 8082de3c), else read back from the source line; an unlabelled point is `cover@<line>`. These give the `F:` figure. Covergroups are not imported. |
| Expression | Not collected. Shows as `UNSP`. |

LCOV export, and the HTML built from it, has line and branch data only. A line's count is its highest statement count, and each branch construct is one block on the construct's line. Coverview gets no toggle or expression datasets.
