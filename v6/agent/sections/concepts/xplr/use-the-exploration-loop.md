## Use the exploration loop

Repeat this decision cycle:

1. Read `frontier`; use `show` to recover a candidate's absolute configuration.
2. Check `knob-effect` before retrying a knob and `diff` when comparing neighbours.
3. Form one hypothesis and apply one interpretable change outside xplr.
4. Register the experiment with its parent, delta, rationale, and snapshot.
5. Run the real flow and attach the terminal outcome with directed metrics.
6. Read the updated frontier and continue.

The ledger is the shared state, so another agent or machine can continue from the same records.
