## A setup margin repair reports RSZ-0062 on a design with borrowing latches

OpenROAD's `repair_timing -setup -setup_margin` judges each endpoint by its slack. A latch whose data arrives while it is open has a setup slack of exactly 0, so with any margin above 0 every borrowing latch counts as violating, whatever time it has left. The resizer works on each such latch, finds no move that raises its slack, and ends with `[WARNING RSZ-0062] Unable to repair all setup violations`. A real violator is still in the list and still repaired, so nothing is lost but run time; on a design with many borrowing latches the warning is permanent and says nothing. This applies to `post-cts-repair`, `global-route-repair` and `post-route-repair` with a `setup-margin`.

To tell it from a real violator:

- The run's `wns_setup_ps` is not below 0: a real violator leaves a negative worst slack.
- `latch_borrow_margin_ps` is the latches' real margin, and `latch_borrow.rpt` has each latch's; see [Read setup slack on a design with latches](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#read-setup-slack-on-a-design-with-latches).

To avoid the warning, leave `setup-margin` unset or 0 on a design whose latches borrow. The fix, a resizer that measures a latch's margin to its closing edge or leaves borrowing latches out of the margin, belongs in OpenROAD.
