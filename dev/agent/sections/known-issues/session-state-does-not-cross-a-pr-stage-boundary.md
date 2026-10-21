## Session state does not cross a P&R stage boundary

Each stage of a staged run, and each post-route repair round and the `finish` stage of a run with `post-route-repair`, is a new OpenROAD session. It reads the previous stage's database, SDC and saved flow variables. Anything an earlier session held only in memory is gone: Tcl variables, procedures and `rename` wrappers, `set_wire_rc`, `set_layer_rc` and `set_dont_use` from a hook, STA settings outside the SDC, and the resizer's state. rb warns `pnr.hook_commands_not_kept` when a hook redefines an existing command, and logs `pnr.hook_commands_added` for new ones, which later sessions do not have.

Move such state into a `session` hook, which runs at the start of every session. It then also applies to the steps before its original point: a `set_wire_rc` moved there from `after-cts` also changes placement. See [Run Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks).
