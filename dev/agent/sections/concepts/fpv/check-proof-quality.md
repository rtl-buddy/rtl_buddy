## Check proof quality

A green verdict can come from unreachable antecedents, unused logic or over-strong assumptions. Keep the default analyses on and run a negative check.

- **Cone of influence (COI).** With `coi: true` (default), the summary reports the fraction of design cells that can affect at least one assertion, and how many assumptions are disconnected from every assertion. A disconnected assumption constrains nothing that is checked; a connected one is not necessarily needed. Missing Yosys or an analysis error warns and leaves COI unavailable without changing the verdict.
- **Vacuity.** Default on for `bmc` and `prove`, off for `cover` and `live`; override with `vacuity: false`. rtl_buddy runs a cover for each single-line `|->` or `|=>` antecedent and reports unreached antecedents as vacuous, meaning the assertion never fired. Missing results are unknown. The covers run as a separate sby `cover` task. Its engines come from `vacuity_engines:`, which defaults to the `smtbmc` entries of `engines:`, else `smtbmc yices`. A `prove` entry on `abc pdr` therefore keeps its vacuity check, since `abc pdr` cannot run covers.
- **Negative check.** Mutate the RTL, or strengthen a property beyond the design guarantee, and confirm the expected assertion fails. [Mutation testing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/mut/) automates this across a suite.
