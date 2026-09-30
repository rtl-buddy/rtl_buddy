## Test a policy with the mock flow

`rb xplr mock` is a synthetic benchmark with known answers, so you can test an exploration policy without EDA runtime:

```bash
rb --machine xplr mock info --scenario zdt1
rb --machine xplr mock run --scenario zdt1 --register
rb --machine xplr mock score --scenario zdt1
```

- `mock info` returns knob domains, costs, infeasible combinations and the analytic ground truth.
- `mock run` deterministically evaluates a knob vector. `--noise` adds seeded objective noise. `--register` records the experiment and outcome together; without it, pass the payload's `outcome` to `attach-outcome`.
- Scenarios: `rastrigin` (single-objective WNS maximization) and `zdt1` (LUT/delay minimization).
- `mock score` reports regret for one objective, or hypervolume and distance-to-front for several.
- Outside a Git repository, `--register` needs `--source-sha` and optionally `--source-branch`.
