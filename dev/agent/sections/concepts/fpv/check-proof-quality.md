## Check proof quality

A green verdict can still result from unreachable antecedents, unused logic, or over-strong assumptions. Keep the default analyses enabled and perform a negative check.

**Cone of influence.** With `coi: true` (default), rtl_buddy uses Yosys to report the design-cell fraction in the backward cone of at least one assertion, including per-module detail in machine results. Missing Yosys or an analysis error warns and leaves COI unavailable without changing the primary verdict.

**Dead assumptions.** The COI pass also counts assumptions whose input logic intersects an assertion cone. Clock and reset network edges are excluded. A reported dead assumption is structurally disconnected, but an assumption reported as used is not necessarily semantically necessary. Assume-to-assume chains are not followed to a fixpoint.

**Vacuity.** For `bmc` and `prove`, vacuity checking defaults on. rtl_buddy derives a cover for each single-line `|->` or `|=>` antecedent and runs a secondary cover proof. Unreached antecedents are reported as vacuous; missing results are unknown. Clocking and same-line `disable iff` clauses are retained. Sequence antecedents are treated as boolean reachability conditions. Override with `vacuity: false`; `cover` and `live` default it off.

**Negative check.** Deliberately mutate the RTL or strengthen a property beyond the design guarantee and confirm that the expected assertion fails. Use [mutation testing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/mut/) to automate this across a suite.
