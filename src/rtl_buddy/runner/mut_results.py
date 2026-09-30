"""Result records for an ``rb mut`` campaign: one ``MutantOutcome`` per mutant and an aggregate ``MutResults``.

The mutation score is ``killed / (killed + survived)``; ``errored`` mutants (those that broke elaboration) are excluded.
"""

from __future__ import annotations

import pprint
from dataclasses import dataclass, field

KILLED = "killed"
SURVIVED = "survived"
ERRORED = "errored"


@dataclass
class MutantOutcome:
    mutant_id: str
    operator: str
    outcome: str  # one of KILLED / SURVIVED / ERRORED
    diff_summary: str
    verdict: str  # the FPV verdict string for this mutant (PASS/FAIL/NA)
    # Signals the operator predicted it would perturb (xeno Prediction.perturbs_signals).
    predicted_signals: list[str] = field(default_factory=list)
    # Model-relative file the mutant was spliced into; set only for scoped (multi-file) campaigns.
    file: str = ""

    def is_predicted_observable_miss(self) -> bool:
        return self.outcome == SURVIVED and bool(self.predicted_signals)


class MutResults:
    def __init__(
        self,
        name: str,
        outcomes: list[MutantOutcome],
        baseline_verdict: str,
        per_file: dict[str, dict[str, int]] | None = None,
    ):
        self.name = name
        self.outcomes = outcomes
        self.baseline_verdict = baseline_verdict
        # Per-file killed/survived/errored counts; None for single-file campaigns.
        self.per_file = per_file

    def killed(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == KILLED)

    def survived(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == SURVIVED)

    def errored(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == ERRORED)

    def scored_total(self) -> int:
        return self.killed() + self.survived()

    def score(self) -> float | None:
        """Return ``killed / (killed + survived)``, or None when no mutant was scorable."""
        total = self.scored_total()
        if total == 0:
            return None
        return self.killed() / total

    def predicted_observable_misses(self) -> list[MutantOutcome]:
        return [o for o in self.outcomes if o.is_predicted_observable_miss()]

    def is_pass(self) -> bool:
        # A campaign passes when it produced a scorable result; score thresholds are not applied here.
        return self.score() is not None

    def as_report(self) -> dict:
        """JSON-serialisable report consumed by ``rb mut score``."""
        report = {
            "name": self.name,
            "baseline_verdict": self.baseline_verdict,
            "killed": self.killed(),
            "survived": self.survived(),
            "errored": self.errored(),
            "score": self.score(),
            "mutants": [
                {
                    "mutant_id": o.mutant_id,
                    "operator": o.operator,
                    "outcome": o.outcome,
                    "verdict": o.verdict,
                    "diff_summary": o.diff_summary,
                    "predicted_signals": o.predicted_signals,
                    "file": o.file,
                }
                for o in self.outcomes
            ],
        }
        if self.per_file:
            report["per_file"] = self.per_file
        return report

    @classmethod
    def from_report(cls, report: dict) -> "MutResults":
        outcomes = [
            MutantOutcome(
                mutant_id=m["mutant_id"],
                operator=m["operator"],
                outcome=m["outcome"],
                diff_summary=m.get("diff_summary", ""),
                verdict=m.get("verdict", "NA"),
                predicted_signals=m.get("predicted_signals", []),
                # "file" is absent in reports from single-file campaigns.
                file=m.get("file", ""),
            )
            for m in report.get("mutants", [])
        ]
        return cls(
            name=report.get("name", "mut"),
            outcomes=outcomes,
            baseline_verdict=report.get("baseline_verdict", "NA"),
            per_file=report.get("per_file"),
        )

    def __str__(self):
        return "mut_results: " + pprint.pformat(self.as_report())
