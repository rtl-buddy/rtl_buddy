## Vacuity covers

A property `a |-> b` is *vacuously true* whenever the antecedent `a` never holds — the assertion passes but tells us nothing about `b`. `rb fpv` auto-derives a `cover property` for each `|->` / `|=>` antecedent in your property set and runs a secondary sby cover-mode pass to check whether each one is reachable.

The vacuity pass is enabled by default for `mode: bmc` and `mode: prove` and disabled for `mode: cover` / `mode: live` (where the user is already exploring reachability). Override with `vacuity: true` / `vacuity: false` per verification in `fpv.yaml`.

When vacuity reports any unreachable antecedent, the results table grows a **Vacuity** column:

```text
FPV Run     Result   Description                ...   Vacuity
counter_inv PASS     property proved (bmc, depth 32)        1/3 vacuous
```

- `N ok` — every antecedent reached
- `M/N vacuous` — `M` antecedents never reached → those `|->` properties are vacuously true (fix your assumptions or your stimulus)
- `K unknown` — sby's cover output didn't tag this cover either way (logfile missing, sby died)

Per-antecedent detail is preserved in `FpvResults.results["vacuity"]["covers"]` for machine consumers and reported in the log:

- `cover_vacuity_<N>_<label>: cover property (<clocking> <antecedent>);` — synthesized into `vacuity_covers.sv`
- `vacuity.log` — full secondary sby pass log
- `vacuity_workdir/` — sby workdir from the cover pass

Scope today:

- Single-line antecedents only (the most common case).
- Clocking and `disable iff` clauses on the same line as the implication are preserved.
- Sequence-valued antecedents (`(req ##2 ack) |-> done`) are extracted but treated as boolean for the cover — close enough for the reachability signal.
- Multi-line antecedents land in a follow-up.
