## Troubleshooting

| Symptom | Action |
|---|---|
| Run fails before starting and lists solver versions | Install the pinned versions or change `solver-versions` |
| `sby` not found | Put it on `PATH` or set its path in `cfg-fpv-tools` |
| `frontend: verilog` fails with no assert, assume or cover cells | The properties did not elaborate; switch to `frontend: slang` or simplify them |
| Warning that a user `+define+FORMAL` was dropped | Remove it. `FORMAL` is always defined |
| Warning that a define was dropped for whitespace | Remove whitespace from the define value |
| Warning that COI is unavailable | Install Yosys or read the error; the verdict is unaffected |
| The whole property read aborts | One construct is unsupported by the frontend build; probe constructs one at a time |
| `wave-fpv` errors | The verification has not run or produced no trace |
