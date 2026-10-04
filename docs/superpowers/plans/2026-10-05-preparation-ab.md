# Preparation A/B implementation plan

**Status:** design only; wait for the first explicit approval before implementing
or invoking agents. The authoritative hypotheses, task list, n, budgets, analysis
and stop rules are in [the preregistration](../../preparation-ab-preregistration.md).

**Goal:** measure whether literal preparation increases accepted work per allowance
with realistic deterministic targets, while exposing wrong-target harm and failures.

**Architecture:** add a dedicated preparation adapter and statistical report beside
`bench.py`. Reuse existing provider invocation, fixture isolation and packet builder
where their contracts fit. Make only narrow dispatch/schema additions to existing
bench commands; preserve historical variants/results. Zero new runtime dependencies,
frozen typed records, pure selector/analysis functions, English comments and explicit
parameters. No Swift changes or general refactor.

## 1. Stage 1 completion (no generation)

Owned files for this commit:

- `docs/preparation-ab-preregistration.md`
- `docs/superpowers/plans/2026-10-05-preparation-ab.md`
- `docs/evaluation.md`

- [x] Inspect main's current preparation, cost, quota and exclusion contracts.
- [x] Read historical measurements and current read-only Codex limits/quota.
- [x] Specify six tasks, four arms, eight-episode pilot and fixed full schedule.
- [x] Separate measured five-hour coefficients from unidentifiable weekly weights.
- [x] Run the five mandated repository verification gates: 87 pytest tests, both
  ruff checks, mypy's 48 source files, and 20 mod tests passed.
- [x] Commit only the named owned files with repo-config noreply email.
- [ ] Present design/budget in Turkish and stop for the first explicit approval.

## 2. Frozen fixtures and no-model eligibility (after first approval)

Create `bench/tasks/preparation-ab-mi-count.json`,
`preparation-ab-boltons-ranges.json`, `preparation-ab-id-salt-rotation.json`,
`preparation-ab-id-mixed.json`, `preparation-ab-id-stale.json`, and
`preparation-ab-id-memory.json`. Store immutable task/step metadata, oracle/wrong
targets, public regression fixtures and reference/hidden acceptance specifications
in a typed companion representation, with hashes in the run manifest. Hidden
validation is generated only after agent subprocess exit outside the agent workspace.

- [ ] Resolve refs and verify all three commit SHAs. Pin Python and dependency
  versions; record an isolated src-layout setup for Itsdangerous.
- [ ] Prove original/gold passes, mutations fail, targeted mutation sites are unique,
  and reference checks distinguish byte fixes from the specified T5 refactor.
- [ ] Establish per-step public test baselines and detect agent changes independently
  of host-injected regression tests. Prove hidden files/reference history are absent
  from execution workspaces and never enter prompts or later feedback.
- [ ] Validate packet sizes, wrong-target disjointness/size matching and the T5
  post-edit helper transition. If the helper is missing, record a failed episode
  with remaining steps uninvoked, never repair it on behalf of the agent.
- [ ] Freeze exact seeded schedule, config, calibration coefficients and artifact
  schema before generation. Commit an amendment for local eligibility corrections;
  seek renewed approval for changes to study scope or allowance.

## 3. Preparation adapter and provider accounting

Create `src/cimrihook/bench_preparation.py`. Extend `src/cimrihook/bench.py` and
`src/cimrihook/cli.py` only at task/schema and command dispatch boundaries.

- [ ] Implement pure evidence selector using only failure traces/names and source
  inventory; immutable candidates/rejections, deterministic order and explicit
  empty selection. Do not read gold/mutation descriptions in auto selection.
- [ ] Capture full initial public failure output and share it across arms; in later
  dependent steps capture the current episode's output and retain any state-induced
  evidence differences. Remove oracle scope from control prompts.
- [ ] Construct oracle/auto/wrong packets through `preparation.py`; separate common
  evidence bytes from source budget, validate current hashes before every step.
- [ ] Run each episode in a fresh lowercase `/tmp/cimrihook-bench/` workspace and
  CLI session. Carry session only within T5/T6, and initial constraints only in
  the first T6 prompt. Freeze tools/hooks/effort and threshold scope consistently.
- [ ] Archive invocation arguments, prompts, packets, cache/input/output/reasoning,
  response IDs, compaction events and provider costs without request duplication.
  Recover measured consumption even when timeouts/turn ceilings fail acceptance;
  never use existing zeroed `failed_result` as a measured zero.
- [ ] Collect native quota before, during and after each invocation. Enforce >85%
  weekly hard stop with process-group termination and cancellation of queued work;
  prevent a launch at 85%, unavailable/stale readings or exhausted allowance.
  Detect reset boundaries and preserve interrupted attempts; no automatic resume.

Proposed interface: `bench-run --preparation-study <manifest>` selects this adapter
using the existing command; it must conflict with generic variants/resume/concurrency
options instead of silently combining protocols. A `--plan-only` path must produce
the complete schedule and consumption arithmetic without launching provider children.
Normal `bench-run` behavior remains unchanged. Exact interface is to be validated
against the current parser during implementation, not assumed to exist now.

## 4. Dedicated report and minimal local verification

Create `src/cimrihook/bench_preparation_stats.py`; add minimal cases to
`tests/test_bench_records.py` and create `tests/test_bench_preparation.py` for stable
pure transformations and real-process/file integration boundaries.

- [ ] Pair by task/agent/repeat, sum costs across steps, retain failed attempts and
  integrity/reference checks. Unknown-cost or incomplete study blocks the primary CI.
- [ ] Implement seeded task-cluster bootstrap, cost/acceptance differences and
  supplementary accepted-work-per-cost measure; no historical Welch inference.
- [ ] Report Codex frozen measured points plus legacy 6/8/20 units, weekly pooled
  interval and calibration sensitivity distinct from task uncertainty. Label
  Claude provider USD versus measured or non-attributable account window deltas.
- [ ] Integrate `bench-report` for manifest-tagged new result sets; keep generic
  failure-filtering behavior from affecting this analysis. Reuse `bench-calibrate`
  only for a clearly separate compaction diagnostic when authorized.
- [ ] Verify selector ambiguity/path rejection, empty evidence, post-edit hashes,
  hidden validation isolation, test tampering, failure cost retention, duplicate
  counters, deterministic schedule/bootstrap and reset handling.
- [ ] Verify process-group stop/queue cancellation with real local subprocesses
  and frozen recorded quota sequences; no subscription generation in these checks.
- [ ] Verify all arm cwd markers stay excluded from doctor/limits. Do not alter
  existing account fitting to include benchmark or preparation runs.
- [ ] Run mandated gates before the pilot and inspect named-file diff.

## 5. Approved measurement pilot, then second stop

- [ ] Re-read native quota and confirm the first approval and pilot allowance.
- [ ] Invoke T3 once per arm per agent, serially in the preregistered orders:
  eight episodes / eight generation invocations at maximum, no retries.
- [ ] Verify complete counters, host/hidden/reference acceptance, isolation and
  all four arm artifacts. Retain errors and exhausted-budget attempts.
- [ ] Run `bench-report`, check host reports against raw artifacts, and update
  `docs/evaluation.md` with n=1, no efficacy CI and actual consumed/predicted points.
- [ ] Re-estimate the full run from pilot costs including failures, with explicit
  remaining headroom and longer-task uncertainty. Commit named owned files only.
- [ ] STOP: show pilot results and revised full-study budget; request second approval.

## 6. Full study only after explicit second approval

- [ ] Refresh quota and allowance; run only the fixed 96 episodes / at most 160
  generation invocations with all stop rules active. No effect-driven stopping.
- [ ] Generate the task-cluster CIs, per-task/all-arm outcomes and sensitivity
  tables, retaining failed and partial episodes. Incomplete studies stay labeled so.
- [ ] Update evaluation with n, CI, actual quota observations, predictor estimates,
  unresolved uncertainties, and established-versus-pilot limits.
- [ ] Run mandatory gates; commit only named owned files and report hashes in Turkish.
  Push, PR and tags remain separate user approval actions.

## Verification gates

These are local repository checks; mod tests invoke plugin validation/tests without
a model generation. Run each command and report actual results:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run python tests/run_mod_tests.py
```

For commits use explicit owned paths with `git add <named-paths>` and the configured
`45736551+senoldogann@users.noreply.github.com`. Work only in
`/Users/dogan/Desktop/CimriHook-bench`, branch `bench/preparation-ab`; the main checkout
is shared. No global package install, paid reviewer agent, push, PR or tag in stage 1.
