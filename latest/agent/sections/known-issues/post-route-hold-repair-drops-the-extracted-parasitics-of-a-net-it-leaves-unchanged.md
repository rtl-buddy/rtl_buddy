## Post-route hold repair drops the extracted parasitics of a net it leaves unchanged

In an `eco` post-route repair round, the repairs time unchanged nets on the extracted SPEF and the nets they change on a placement estimate. On OpenROAD 26Q4, `repair_timing -hold` tries a buffer at a driver and rolls it back when the driver's slew grows by more than 20% or the worst setup slack drops below the setup margin. The rollback leaves the net on the estimate, although it has the driver and loads it had.

Where the estimate is slower than the extraction, the endpoint behind the net reads as met and the repair skips the rest of its path. The round's extraction then reports the endpoint failing by what it failed before, and every later round repeats this.

An `eco` round therefore repairs hold twice: first without a hold margin and with no signal wire RC, so no estimate is slower than its route, then with the margin at the round's RC. See [Post-route repair rounds](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#post-route-repair-rounds). An OpenROAD without `sta::scenes` or the `est::` wire RC commands (before 26Q2), and a session that sets signal wire RC per technology, run both calls at the session's RC, and `pnr.log` says so.
