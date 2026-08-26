## How scoring works

Each generated mutant is classified into one of three outcomes:

- **KILLED** — the verification caught the mutant (good).
- **SURVIVED** — the mutant slipped through every oracle (a coverage gap).
- **ERRORED** — the mutant broke elaboration/compilation, so it could not be scored.

```
mutation score = killed / (killed + survived)
```

`ERRORED` mutants are dropped from the denominator, so a mutant that simply fails to elaborate never inflates or deflates the score. When nothing was scorable (every mutant errored, or none were generated) the score is reported as `n/a`.

A **SURVIVED** mutant whose operator *predicted* it would perturb observable signals is the highest-signal finding — it means a change the engine expected to be observable still passed your checks. These are flagged separately as **predicted-observable misses (weak properties)**.
