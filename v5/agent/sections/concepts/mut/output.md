## Output

`rb mut list` prints a **Mutation Candidates (N)** table with `Operator`, `Line:Col`, and `Snippet` columns.

`rb mut run` prints a **Mutation Testing Results** table with one row per mutant:

| Column | Contents |
|---|---|
| `Mutant` | Mutant id |
| `Operator` | Operator that produced it |
| `Outcome` | `KILLED` / `SURVIVED` / `ERRORED` |
| `Verdict` | The oracle verdict recorded for the mutant (e.g. the FPV proof's `PASS`/`FAIL`/`NA`) |
| `Predicted Signals` | Signals the operator predicted it would perturb (`-` if none) |
| `Mutation` | Short diff summary of the injected change |

…followed by summary lines:

```
Mutation score: 78.6% (killed 11 / scored 14)
Survived: 3   Errored: 2   Baseline: PASS
Predicted-observable misses (weak properties): m07, m12
```

The full report is written to `<mut.yaml dir>/artefacts/mut/<campaign>/mut_report.json` and echoed as `Report written to …`. Its keys are `name`, `baseline_verdict`, `killed`, `survived`, `errored`, `score`, and `mutants[]` (each with `mutant_id`, `operator`, `outcome`, `verdict`, `diff_summary`, `predicted_signals`). `rb mut score <report>` recomputes the score from exactly this file.

### Machine output

Under `rb --machine …` (a global flag, before the subcommand):

- `rb --machine mut list` emits a `sites` array in the JSON envelope.
- `rb --machine mut run` / `rb --machine mut score` emit the full report under `report`.

### Exit codes

`rb mut run` exits `0` when it produced a scorable result and `1` only when nothing was scorable (`score` is `n/a`). **Score thresholding is not gated** — failing under a target score is a separate concern; the command does not exit non-zero just because the score is low. `rb mut list` and `rb mut score` exit `0` on success and fail only on fatal errors (missing engine, missing config, missing report).
