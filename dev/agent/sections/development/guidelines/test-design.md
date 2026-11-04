## Test Design

Each test protects an observable contract or failure mode. Use the lowest layer that proves it, with representative integration tests for CLI, process, tool and protocol boundaries. Keep distinct inputs and failures separate; give parameter cases readable IDs.

Combine related result, artifact and event assertions after the same expensive action. Share immutable inputs or completed outputs through small domain support modules; keep mutable applications, environments, projects and servers isolated. Do not import helpers from collected test modules when adding shared support.

Failure-path tests must prove that the injected failure occurred. Assert exact verdicts, structured payloads and expected outputs; negative assertions must also establish that required output exists. Fakes reject unexpected calls and replace the boundary production actually uses.

Collection performs no external capability subprocesses. Tests control package and tool availability, relevant environment variables and project roots, including ancestor repository markers. Probe optional tools only in fixtures used by their integration tests; retain those integration checks and report their skips.

Bound subprocesses, pipe and socket reads, and thread joins. Clean up children and transports even when an assertion fails. Use events or pipes for ordering and fake clocks for polling arithmetic. Preserve real process, locking, cancellation and compatibility tests where the boundary itself is the contract.

Before deleting a case, identify its retained assertions and compare affected line and branch coverage. Identical execution paths alone do not establish equivalent assertions. Validate important failure-path replacements with a deliberately bad result or fault. Parametrization reduces boilerplate, not the number of executed cases.

Measure collection, setup and calls separately with the same interpreter and tool profile. Compare runtime without coverage instrumentation; keep coverage enabled in CI, including Python subprocesses. Large fixtures and wall-clock assertions need a specific scaling, boundary or resource contract. See [Environment Setup](https://rtl-buddy.github.io/rtl_buddy/dev/development/setup/#tests) for focused, timing and coverage commands.
