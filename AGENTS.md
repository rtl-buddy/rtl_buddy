# rtl_buddy — AI Agent Guide

## Role

This repo is the source-of-truth implementation of the `rtl_buddy` CLI.

## Canonical Guidelines

Read these before opening issues or PRs, or before changing runtime behavior:

- [docs/development/guidelines.md](docs/development/guidelines.md) — engineering rules: execution contexts, path ownership, artifact layout, subprocesses, dependencies, logging, errors, validation, releases, **quirks & known issues**, **issue triage**, and **milestones**.
- [docs/development/docs.md](docs/development/docs.md) — documentation authoring rules.
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — contributor entry point that links to both.

Issue conventions worth knowing up front:

- Type, Priority, and Effort are org-level GitHub Issue Fields, not labels. Templates under `.github/ISSUE_TEMPLATE/` pre-bind Type.
- Area is captured with `area/*` labels, kept consistent across all rtl-buddy repos: `area/test`, `area/wave`, `area/cdc`, `area/fpv`, `area/abv`, `area/mut`, `area/pd`, `area/fpga`, `area/hier`, `area/axi-profile`, `area/hub`, `area/skill`, `area/workflow`, `area/config`, `area/tooling`, `area/infra`. Plus `discussion`. The taxonomy is defined once in `.github/labels.json` and propagated with `.github/sync-labels.sh`; see the guidelines table for what each covers.
- The `version/{patch,minor,major}` labels are PR-only and drive the release workflow. A `version/major` PR **must** add a `## vN to vM` section to `docs/migrations.md` before merge (see guidelines → [Releases](docs/development/guidelines.md#releases)); the label and the migration section are inseparable.
- Close issues automatically: put a closing keyword (`Closes #NN`) in each PR **description**, one line per issue for multi-issue PRs. A number in the title, or a range like `#334-#340`, does **not** autoclose. See guidelines → [Pull Requests](docs/development/guidelines.md#pull-requests).
- Multi-issue long-running efforts get a theme-named milestone (e.g. "Hub Phase 3"), not a version-named one.

Where this file overlaps with the canonical guidelines, treat the guidelines as authoritative.

## Key Files

```text
src/rtl_buddy/
├── __main__.py            # package entry point
├── rtl_buddy.py           # Typer CLI and top-level command flow
├── skill_install.py       # `rtl-buddy skill ...` subcommands
├── skill/                 # bundled agent skill (shipped in the wheel)
├── logging_utils.py       # log_event(), setup_logging(), console helpers
├── errors.py              # FatalRtlBuddyError, FilelistError, SetupScriptError
├── seed_mode.py           # seed handling enum
├── config/
│   ├── root.py            # discover_project_root(), RootConfig
│   ├── model.py           # ModelConfig (models.yaml)
│   ├── test.py            # TestConfig / TestConfigFile (tests.yaml)
│   ├── synth.py           # SynthConfig, SynthSuiteConfig, SynthRegConfig, SynthToolConfig (synth.yaml)
│   ├── spec.py            # SpecConfig / SpecBlock / SpecCoverageItem (specs.yaml)
│   └── ...                # platform, rtl, verible, coverage, coverview, reg
├── graph/
│   ├── config_tier.py     # design knowledge graph, config tier (#376): suites/tests/
│   │                      # testbenches/models/specs/coverage -> node-link JSON
│   ├── build.py           # `rb graph build` (#377): runs every tier, merges, writes
│   │                      # artefacts/graph/graph.json + graph-meta.json
│   ├── merge.py           # node-id-union merge, input hashing, build fingerprint
│   ├── binding.py         # post-merge binding stage (#378): cocotb test -> Python
│   │                      # module -> DUT module, dut.<sig> -> port, test -> golden
│   ├── results.py         # `rb graph results` (#379): per-run result envelopes +
│   │                      # artefact layout -> artefacts/graph/results-overlay.json
│   ├── query.py           # `rb graph query|path|explain` (#380): keyword match +
│   │                      # neighbourhood, shortest paths, one-node explain
│   └── graphify.py        # optional extractor CLI wrapper (binding tier + cross-check):
│   │                      # bundled rtl-buddy-graph-extract, or Graphify (wins if present)
├── mcp/
│   ├── toolset.py         # `rb mcp` (#380) tool specs + handlers; imports NO SDK
│   └── server.py          # the only SDK-importing module: stdio transport
├── runner/test_runner.py  # PRE -> COMP -> SIM -> POST execution
├── runner/synth_runner.py # synthesis dispatch; resolves tool config and invokes backend
├── runner/synth_results.py # SynthResults / SynthPassResults / SynthFailResults / SynthSkipResults
└── tools/
    ├── synth_yosys.py     # Yosys backend: filelist → synth.ys script → yosys invocation
    ├── cdc_rtl_buddy.py   # rtl-buddy-cdc subprocess wrapper, parses JSON report
    ├── hier_rtl_buddy_view.py # rtl-buddy-view subprocess wrapper for `rb hier` / `rb hier-query`
    ├── spec_trace.py      # discover_spec_configs, build_coverage_map, etc.
    └── ...                # filelist, sim, postproc, verible wrappers

scripts/
└── graph_token_benchmark.py  # graph route vs raw route token benchmark (#381)
```

## Implementation Notes

- `rtl_buddy.py` owns CLI wiring, global options, and command dispatch.
- `RootConfig` selects platform, builder, verible, and regression config from `root_config.yaml`.
- `TestRunner` drives PRE, COMPILE, SIM, and POST with early-stop support.
- `VlogSim` captures the suite cwd once, but both compile and sim now run from per-test workspaces under `artefacts/<sanitized-test>/`; repeated runs use `artefacts/<sanitized-test>/run-0001/`, while `test.log`, `test.err`, and `test.randseed` in the suite directory remain latest-run symlinks.
- `VlogFilelist` handles `.f` parsing and transformations. It resolves model entries from the real `models.yaml` location, resolves testbench entries from the suite cwd, and writes paths relative to the directory containing the generated `run.f`.
- Nested raw coverage paths such as `artefacts/<test>/run-0001/coverage.dat` must preserve the suite-root hint during LCOV/Coverview `SF:` rewriting. When updating coverage path logic, make sure duplicate basenames still resolve against the originating suite root instead of falling back to repo-wide basename matching.
- Hook scripts (`sweep`, `preproc`, `postproc`) are executed dynamically and should be treated as compatibility-sensitive APIs.
- `SynthRunner` resolves a `SynthToolConfig` from `root_cfg.get_synth_tool_cfg(tool_name)`, merges any `tool_overrides` from the `SynthConfig`, then dispatches to `YosysSynth`. Opts resolution: root-config `opts` are the baseline; per-run `tool_overrides.<tool>` keys overwrite matching fields.
- `YosysSynth` writes `synth.f` via `VlogFilelist` (with `unroll=True, strip=True, deduplicate=True`), then generates `synth.ys`. Source files are emitted as individual `read_verilog -sv -defer` commands (not `-f filelist`) so Yosys only elaborates the top hierarchy. Pass/fail is determined by exit code then `ERROR:` line scan.
- `rb hier <model>` (`tools/hier_rtl_buddy_view.py`) writes a stripped+deduplicated filelist to `artefacts/hier/<model>/hier.f`, then shells out to `rtl-buddy-view` with `--top <model> --filelist hier.f --format <fmt>` plus optional `--output`, `--frontend`, `--cdc-annotations`, `--clock-legend`. The renderer's stdout passes through to the terminal when `-o` is not given (so `rb hier x --format dot | dot -Tsvg ...` works); stderr is captured to `hier.log`. The integration is at subprocess granularity — rtl_buddy is not coupled to the viewer's Python API. The viewer's JSON contract (`schema_version`, `tool.*`, `design.top`, `nodes`, `edges`) is guarded by `test_json_contract_keys_present_and_typed` in rtl-buddy-view.
- `rtl_buddy.graph.config_tier` extracts the **config tier** of the design knowledge graph (#375/#376) by reading the existing loaders and `tools/spec_trace.py` — never a second YAML parser, so the graph cannot disagree with `rb spec check-coverage` / `check-design`. It emits NetworkX node-link JSON; the node/edge vocabulary, the id conventions, and the `module:<name>` stitch to rtl-buddy-view's design tier are documented in `docs/concepts/graph.md`. The entry points are `build_config_tier()` / `extract_config_tier()`. It also reads the five repo-level regression files (`regression.yaml`, `synth_regression.yaml`, `fpv_regression.yaml`, `cdc_regression.yaml`, `fpga_regression.yaml`, each through its own `*RegConfig`) for **flow provenance**: every suite/test/testbench node carries a `flow` stamp, a suite no regression claims defaults to `sim`, and the four non-simulation flows' suites — which no `verif/` walk reaches — become `suite:`/`test:` nodes of the same types and ids a `tests.yaml` produces, with `exercises` starting at the run (there is no testbench) and the run's `top:` giving a `targets` stitch — the run's own third of the config->design edge, beside a model's `maps_to` and a testbench's `elaborates_as`. Those files join the config tier's input hashes, so wiring a suite into a flow invalidates the build fingerprint. `rb graph results` cross-checks against **simulation** test nodes only — a synthesis run leaves no result envelope, and counting it would make `--strict` fail on any multi-flow project.
- `rb graph build` (`graph/build.py`, #377) is the orchestrator: it runs `rtl-buddy-view graph` once per model through `RtlBuddyViewGraph` (sharing `rb hier`'s `artefacts/hier/<model>/hier.f`), extracts the config tier in-process, optionally runs the extractor's binding tier (`resolve_extractor()`: Graphify wins when installed, else the bundled `rb-graph-extract` from the `graph-extract` extra — the manifest name of whichever ran is the fingerprint key), and unions the tiers with `graph/merge.py` into `artefacts/graph/graph.json` + `graph-meta.json`. Three rules hold it together: the merge is **internal** (`merge_graphs()`, node-id union) because the extractor is optional and its `merge-graphs` is only ever a cross-check; a re-run whose fingerprint (input hashes + tool versions + schema version, stored in the sidecar) still matches is a **no-op**; and a missing or broken optional tier degrades to a `skipped`/`failed` row in the envelope rather than sinking the build. The design tier is version-gated on `rtl-buddy-view >= VIEW_GRAPH_MIN_VERSION` (`graph/build.py`), the same shape as the `hier-query` gate, with dev/editable builds trusted over the floor. The design tier is exported **twice per design**: once rooted at the model, and once per testbench rooted at its `toplevel:` (`--tb-top`, reusing `rb hier --view tb`'s DUT+TB filelist merge) — `testbenches_from_suites()` picks the targets, dedupes on `(model, suite, tb filelist, top)` and drops any testbench whose top *is* the DUT top (cocotb/SystemC, where the DUT export already covers it). Three rules there: the weld is the shared `module:<name>` id, so a TB instance's `instance_of` lands on the DUT-rooted export's node; a `tb:` node reaches its hierarchy through `elaborates_as` — the same relation the model's `maps_to` states, spelled with the testbench's own verb so the source kind is read off the edge instead of off the id prefix — declared from `toplevel:` in the config tier and otherwise from the top the viewer actually elaborated, where the observation replaces the declaration; and because a SystemVerilog module name is only unique inside one elaboration (every suite calls its top `tb_top`), any design-tier id claimed by two different **files** gets the testbench copies qualified with their suite (`module:tb_top@verif/template`), with `label` untouched, DUT ids never qualified, and every qualification listed in `graph-meta.json` under `id_collisions`.
- `rtl_buddy.graph.binding` is the **binding stage** (#378) — the only part of `rb graph build` that runs *after* the merge, because it is the only part that needs both halves at once: the config tier says which cocotb module belongs to which `toplevel:`, and the design tier owns the `port:<top>.<name>` nodes a `dut.<name>` access resolves against. It emits `binds_to` (test→Python module→`module:`), `imports` (Python module→Python module), `drives` (Python module→`port:`) and `checks_against` (test→`golden_model`). Three rules: `dut.<name>` is found with `ast` and not a regex (the regex is only the fallback for a file that will not parse), so `"dut.a"` in a string is not an access; **`drives` is the one edge class in the whole graph allowed to be INFERRED**, and the port table alone decides — an exact port-name match is EXTRACTED, everything else is INFERRED with `resolved: false` when it is known not to be a port; and a fact inherited through a helper module carries `via` naming that helper, so second-hand evidence never masquerades as first-hand. The output is a fourth graph merged in on a second pass, kept at `artefacts/graph/bind/graph.json` apart from the extractor's `binding/graph.json`. When an extractor is installed its Python node ids are adopted (matched on the node's repo-relative `file`) instead of a second `py:<path>` node being invented for the same file.
- `rtl_buddy.graph.results` is the **volatile half** the graph deliberately excludes (#379). `rb graph results` writes `artefacts/graph/results-overlay.json` keyed by the config tier's own `test:<suite dir>#<name>` ids; `graph.json` is read for the id cross-check and never written, which is what keeps it hash-stable across regressions (the fingerprint is safe too — artefact dirs are in no tier's input list). Three rules: the status comes from the **result envelope** (`runner/result_io.py`), never from a log — so every in-process run now writes one to `<suite>/artefacts/<test>/result.json` (`run-NNNN/result.json` per `randtest` iteration) via `RtlBuddy._record_run_results()`, best-effort, alongside the dispatch path's `dispatch/result-<tag>.json`; the `timestamp` is the **envelope file's mtime**, never a wall clock, so a refresh with nothing re-run is byte-identical; and a test with artefacts but no envelope is reported `UNKNOWN` with its paths rather than omitted. `load_overlay()` / `overlay_for_node()` / `annotate_graph()` are the join hooks `rb graph query` (#380) uses.
- `rtl_buddy.graph.query` is the **read side** (#380) behind `rb graph query|path|explain` and the `rb mcp` graph tools. Three rules: matching is a **fixed keyword rubric, not a model** (exact id > exact label > whole-word > substring; id beats file path; ties break on the node id), because a graph whose search costs an LLM call has given back the tokens it saved; a word naming a node type (`tests`, `module`, `ports`) is pulled out of the search terms and becomes a **preference that can promote but never conjure** a match, which is why "which tests cover A-COV-1" still finds the coverage item the tests hang off; and `path` is **undirected by default** because edge direction here encodes role, not reachability. Expansion follows edges both ways — `covers` runs test→item and half the questions read it backwards. The overlay is joined onto every node in every payload (`results`) and never written back. `cite_hint()` turns an `inst:<top>/<dot.path>` id into the `rb hier-query <top> source-snippet <path>` command that quotes it, which is the "locate then cite" contract made mechanical. `load_context()` is the one loader; a missing graph raises `GraphQueryError` naming `rb graph build`, a missing overlay is never an error.
- `rtl_buddy.mcp` is `rb mcp`, the stdio MCP server (#380) — the second LLM-facing surface next to `--machine`. Four rules: `mcp/toolset.py` **imports no SDK** (plain dataclasses + JSON Schema literals), so the tool set builds, lists and answers on a machine without the optional `mcp` extra and the schemas stay testable there; `mcp/server.py` is the only SDK importer, lazily and behind `require_sdk()`, and adapts both the 1.x decorator API and the 2.x constructor-callback API; every tool returns its `rb --machine` counterpart's **payload verbatim** inside `{tool, ok, meta.rtl_buddy_version, payload}` because two surfaces with two shapes drift within a release; and a bad question is `ok: false` plus a message, never an exception — a transport error teaches an agent to stop asking. The server is **stateless and daemon-free**: each call re-reads `graph.json` + the overlay and shells out to `rtl-buddy-view`, so a CI or dispatch node answers "what instantiates X?" without a hub. Hub tools (`hub_state`/`hub_select`/`hub_open_source`/`hub_resolve`/`hub_diagnose`) are registered only when `discover_hub()` says a hub is live, and that decision is delegated to `hub.client._discover_hub_addr` rather than re-derived, so the advertised tools and `rb hub send` can never disagree. `RtlBuddyViewQuery(capture=True)` exists for this server: under stdio, a passed-through viewer stdout would land in the JSON-RPC stream.
- `rtl_buddy.hub.graph_page` is the **look-at-it half** of the epic (#382): `GET /graph.json` serves `graph.json` joined with `results-overlay.json` through the same `annotate_graph()` the query verbs use, and `GET /graph` serves `graph_page.html` — one self-contained document (no CDN, no external stylesheet, no build step; the hub is routinely run with no route off localhost). Three rules: the join is **in memory**, so the pane can never be the thing that makes `graph.json` churn across regressions; the pane is a **hub peer with its own `Origin.GRAPH`**, not a second `view`, because the hub allows one client per origin and the whole point is clicking a module in the graph to select it in the schematic *that is open at the same time*; and a node click emits the **same envelopes the SPA emits** (`selection_changed` for anything that resolves to an instance path — a `module` node resolves via the shallowest instance of it — and `open_source` for anything that knows its `file`), so the pane adds no protocol of its own. `graph_focus` is the one new wire type, driven by `rb hub send graph-focus <node>` and cached in `HubState` so it replays to a pane that connects afterwards. The pane's eight columns are **not** tiers — a tier says which tool made a node, and `design` + `config` between them hold the spec, the DUT, four flows' suites and every testbench hierarchy; `categorize_nodes()` buckets by (tier, type, `flow`, `cocotb`, testbench-ownership) into `spec | design | test-config | syn-config | formal-config | cdc-config | test-cocotb | other`, stamps it as `category` in the **served** payload only (writing it to disk would be exactly the churn #379 exists to prevent), and identifies a testbench-side design node by `qualified_by`, by being a `tb:` node's `elaborates_as` target that no `model:` `maps_to` as well, or by an `inst:<root>/…` id rooted at one of those — a module under a testbench root that is none of those stays in `design`, because over-claiming would file a vendor IP block under someone's testbench.
- `scripts/graph_token_benchmark.py` is the epic's **success gate** (#381): six agent questions answered twice — once through `rb --machine graph query|path|explain`, once through grep-and-read — with a hand-checked answer key and `len(text) // 4` as the token proxy, charged on the command as well as the output. Four rules: **correctness before cost** (a route that answers wrong is not the cheap one, and all twelve runs are asserted against `EXPECTED`); **no privileged access** (each route may only use text its own `Route` handed back, so the count is the cost of the answer and not of the script); **repetition is free on both routes** (`machine()` caches like `read()` — an agent does not re-run a query still in its transcript); and `rb graph build` is **not charged**, being an index amortized per source change. Four tasks are everyday lookups; two exist to test the structural hypothesis — a five-hop chain (coverage item → tests → testbenches → DUT → spec doc + golden) and change impact on `ip_cdc_sync`, the IP five other elaborations pull in. The measured table lives in `docs/concepts/graph.md#token-efficiency`, stamped with the template commit it came from. **The hypothesis did not survive**: the graph route costs ~3x *more* across all six, and the itemized `--json` says why in one line — an `explain` payload costs 440–6 174 tokens (median 946) whatever it describes, while the template's files cost 133–2 019 (median 341), so a hop only pays when it replaces a file bigger than itself. Change impact is the closest (0.49x) and the only task where the graph needs *fewer calls* than grep (14 vs 24), because elaboration already computed the closure that costs grep a three-round fixpoint and two comment-only false-positive reads. Its inclusion criterion — a run counts when a **project-root** regression manifest claims its suite and the elaboration contains the IP — is stated in the script and the docs; the template's CDC analyses fall outside it because `cdc_regression.yaml` sits at `lint/cdc/`, which root-filename flow discovery cannot see. `tests/test_graph_benchmark.py` guards the proxy, the key and the raw route's parsers in CI, and runs both routes end to end when `RTL_BUDDY_TEMPLATE_ROOT` points at a built project.
- `rb hier-query <model> <verb> <arg>` (same wrapper module, `RtlBuddyViewQuery`) shells out to `rtl-buddy-view query {find-module,subtree,instances-of,port-connections,source-snippet}` (rtl-buddy-view ≥ 0.3.0) and prints the JSON / snippet answer on stdout. It shares `rb hier`'s `artefacts/hier/<model>/hier.f` filelist; unlike `rb hier` it streams the viewer's **stderr through to the terminal** (a lookup miss is the answer, not a diagnostic) and logs the invocation to `query.log`.

## Validation

For validation policy (what to run for which change), see [Validation](docs/development/guidelines.md#validation) in the engineering guidelines. Concrete commands:

```bash
# from a project root that has `rtl_buddy` installed
./venv/bin/python -m rtl_buddy regression -c regression.yaml
./venv/bin/python -m rtl_buddy filelist test_module -c design/example_block/src/models.yaml
./venv/bin/python -m rtl_buddy verible syntax design/example_block/src/test_module.sv
./venv/bin/python -m rtl_buddy --machine docs list
./venv/bin/python -m rtl_buddy --machine docs show agents

# from a suite directory
cd design/example_block/verif
../../../venv/bin/python -m rtl_buddy test basic
```

If validating the dev checkout directly, install this repo into the target venv and confirm with `./venv/bin/python -m rtl_buddy --version`.

## Code Quality

This repo uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. CI enforces both on every PR via `.github/workflows/lint.yml`.

Install the pre-commit hook once after cloning so Ruff runs automatically on every commit:

```bash
uv tool install pre-commit
pre-commit install
```

To run Ruff manually:

```bash
uv run ruff check          # lint
uv run ruff format         # format in place
uv run ruff format --check # check only (what CI does)
```

To update the pre-commit hook version:

```bash
pre-commit autoupdate
```

## Testing

The `pytest` suite under `tests/` is run in CI by `.github/workflows/test.yml` with coverage on every push and PR. Locally:

```bash
uv run pytest                                    # run the suite
uv run pytest --cov                              # with coverage summary
uv run pytest --cov --cov-report=term-missing    # show uncovered lines
uv run pytest --cov --cov-report=html            # write htmlcov/index.html
```

Coverage configuration lives in `[tool.coverage.*]` in `pyproject.toml` (source = `src/rtl_buddy`, excludes the bundled `skill/` and `docs/`). No `--cov` is set in `pytest.ini` so plain `pytest` stays fast; pass `--cov` explicitly when you want a coverage run.

## Logging and Error Handling

Policy lives in [Logging](docs/development/guidelines.md#logging) and [Error Handling](docs/development/guidelines.md#error-handling). Code-level helpers and entry points in this repo:

- `log_event(logger, level, "event.name", **fields)` in `src/rtl_buddy/logging_utils.py` — the only sanctioned runtime-logging call. Do not use `logger.info(f"...")` directly.
- `_human_message()` in the same module — add a `case` entry for any new WARNING or ERROR event so the human-mode message stays clear.
- Exception classes: `FatalRtlBuddyError` (top-level `run()` exits with code 2), `FilelistError` (caught by `TestRunner`, becomes `FilelistFailResults`), and the setup-failure string contract from `pre()` / `_expand_tests_with_sweep()` (becomes `SetupFailResults`).
- Do not use `logger.critical()` — the old `ExitHandler` abort pattern has been removed.
- Console helpers: `emit_console_text()` for direct user-facing output, `render_summary()` for result tables (Rich on console, plain text in the log), `task_status()` for spinners on long-running phases.

## Skill Distribution

The rtl_buddy agent skill ships inside this wheel at `src/rtl_buddy/skill/` and is materialized by `rtl-buddy skill install`. There is no separate skill repo — the legacy `rtl-buddy-codex-skill` repo is deprecated. Dev-only audit skills live under `.claude/skills/` in this repo and are not distributed.

### Rules when editing skill content

- `src/rtl_buddy/skill/SKILL.md` is the single source consumed by both Claude Code (at `.claude/skills/rtl_buddy/`) and Codex (at `.agents/skills/rtl_buddy/` for project scope, `~/.codex/skills/rtl_buddy/` for user scope).
- Keep `SKILL.md` ≤60 lines and agent-specific. Anything covered by the docs site should cite <https://rtl-buddy.github.io/rtl_buddy/>, not restate it.
- Agent-facing local docs access goes through `rtl-buddy docs ...`. The wheel ships `docs/**/*.md` directly (via a symlink at `src/rtl_buddy/docs`) so docs are always in sync with the installed version.
- Any edit to `SKILL.md` takes effect for users only after they re-run `rtl-buddy skill install`. `rtl-buddy skill status` surfaces stale installs via the `.rtl_buddy_skill_version` marker.
- `src/rtl_buddy/skill/gitignore_snippet.txt` is printed by project-level installs and by `rtl-buddy skill print-gitignore`.
- Files in `src/rtl_buddy/skill/` are included in the wheel automatically via hatchling's `packages = ["src/rtl_buddy"]`. Adding new files to the skill dir requires no extra config. The `docs/` directory ships via `force-include` in `pyproject.toml` and is excluded from package discovery to avoid double-inclusion.

### Install scope policy

- **Default is user-level** (`~/.claude/skills/rtl_buddy/`, `~/.codex/skills/rtl_buddy/`). This is deliberate: the skill is workflow-pattern guidance that changes rarely across rtl_buddy versions, and a single copy per machine nudges users to keep rtl_buddy versions aligned across projects.
- `--project` (or `--root PATH`) opts into project-level install at `<root>/.claude/skills/rtl_buddy/` and `<root>/.agents/skills/rtl_buddy/`. Claude Code's project-level precedence means a project-level copy overrides the user-level one when both exist — this is the escape hatch for projects that pin a divergent rtl_buddy major.
- Do not flip the default to project-level without rediscussion; the precedence model makes user-level-plus-project-override the clean path.

### Project root discovery

`config.root.discover_project_root()` is the single shared entry point for locating the project root. It walks up from `cwd` for `root_config.yaml`, then for `.git/`. Pass `fallback_cwd=True` to return `cwd` silently when neither is found (used by the spec commands); the default raises `FatalRtlBuddyError`. This handles agents invoking from `verif/<suite>/` subdirs — `Path.cwd()` alone would be wrong.

## Release Workflow

Release policy (when stable vs pre-release, do-not-do rules) lives in [Releases](docs/development/guidelines.md#releases). This section covers what the workflow does mechanically.

### Stable release

Triggered by merging a PR to `main` with a `version/{patch,minor,major}` label. On merge:

1. The workflow computes the next `vMAJOR.MINOR.PATCH` tag, creates it, and pushes it.
2. A GitHub release is created (not marked pre-release).
3. The wheel is built (hatch-vcs derives the version from the tag) and published to PyPI.
4. Docs are deployed to `gh-pages` under the matching `v{major}` alias; `latest` is updated if this is the highest major.

### Pre-release

Cut from a feature branch via `workflow_dispatch` with the **Mark as pre-release** checkbox enabled:

1. The workflow appends `rcN` to the computed base tag (PEP 440). If `v2.3.0rc1` already exists, the next is `v2.3.0rc2`.
2. A GitHub release is created and marked **pre-release**.
3. The wheel is published to PyPI as a pre-release version (e.g. `2.3.0rc1`). Unqualified version ranges (`>=2.2.0`) will not resolve to it.
4. Docs are **not** published — the `latest` alias is not updated.

The version is computed from the latest stable tag at dispatch time. If `main` advances and releases the same bump tier before your branch merges, the next RC will shift to the following version — that is expected and acceptable.

### Infrastructure notes

- GitHub Pages must be configured to publish from the `gh-pages` branch.
- A `GH_PAGES_TOKEN` secret is required because pushes made with the default `GITHUB_TOKEN` do not reliably trigger downstream docs publishing from automation-created tags.
- Update and tag any downstream integrations that track this repo after a stable release.
