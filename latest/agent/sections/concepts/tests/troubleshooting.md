## Troubleshooting

| Symptom | Action |
| --- | --- |
| `rb test` exits 2 before running | Fix the selection: duplicate or unknown name, invalid regex, no `--filter` match, or names combined with `--filter` |
| `Sim hit timeout` | Follow [Triaging `Sim hit timeout`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#triaging-sim-hit-timeout) |
| Result `NA`, exit 1 | The test printed neither `PASS` nor `FAIL`; add a terminal marker |
| Warning that both markers appeared | Print one marker. `FAIL` was used |
| `MULTITOP` error | Declare `toplevel` on the testbench |
| `No rule to make target '.../verilated.cpp'` | Rerun with `--rebuild` to clear objects from another Verilator |
| Stale build reused | Rerun with `--rebuild` after changes rtl_buddy cannot see |
