## A staged P&R run can differ from a one-session run

A staged run runs the same flow commands as a one-session run of the same configuration. Its results are identical at equal thread counts unless OpenROAD carries in-memory state over a stage boundary in the one-session run, which the staged run starts without.

- The resizer keeps state from `repair_design` and `buffer_ports` in memory, so a repair in a later stage can act differently after a stage boundary. The known case is a hook that runs one of them in an earlier stage, such as `repair_design -pre_placement` in an `after-pins` hook. rb's own `buffer-ports` step runs in the `floorplan` stage and its `repair_design` in `place`, and the project template's sky130hd flat block with `buffer-ports: true` routes identically staged and in one session.
- Without the PDK's `rcx-rules`, the final reports of the `finish` session are timed on the global route restored from the database's route guides. It can miss a few pins (`EST-0026`) that the one-session run's route reached, so slack and TNS can differ slightly. With `rcx-rules` the reports are timed on the extracted SPEF and match.

Compare a staged run with a one-session run at the same `threads:` before relying on it to reproduce one. See [Run stages as separate sessions](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions).
