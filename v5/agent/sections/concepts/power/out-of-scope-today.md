## Out of scope (today)

- **Per-instance power breakdown.** The current parser only takes the `Total` line; per-module/per-instance numbers are in `power.rpt` but not surfaced.
- **Multi-corner power signoff.** One corner per run; multi-corner needs a richer schema.
- **RTL-level power estimation** (Joules-style). Would need a different backend; the activity schema would extend without breaking.
