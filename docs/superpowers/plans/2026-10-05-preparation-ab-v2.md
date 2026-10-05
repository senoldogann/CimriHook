# Preparation selector v2 and reduced-study plan

**Status:** v2 local validation complete (6 tasks / 10 steps); reduced one-task
proposal awaits separate approval. User rejected the 96-episode study. Only local
validation and an unapproved reduced design are authorized. No provider generation,
paid reviewers, scheduler, push, PR or tag. Use the bound benchmark worktree.

**Goal:** abstain when failing-test output cannot supply a production line, prove
control-equivalent empty preparation locally, and propose a study within the new
10-point Codex weekly / $15 Claude limits.

**Architecture:** keep the existing packet builder and runner. Filter selection
by the existing evidence rank; add one host-only validation module which reuses
pinned fixtures, public tests, packet construction and gold restoration. Store
selection/overlap data separately from observed agent results. The reduced study
is an explicit schedule draft, not an executable or approved manifest.

## Authorized local changes

- [x] Define rank 100 as the source-selection threshold: a normalized traceback
  path must be in the production inventory and line >0. Rank-50 module matches
  never produce sources, even if another strong candidate exists.
- [x] Preserve candidates and rejection reasons; no model, AST symbol guessing,
  gold target or hidden-test data enters selection. Existing range/budget rules stay.
- [x] Add minimal stable-data checks for weak-only, mixed and production-only
  evidence. Verify empty auto prompt/packet equals control in actual fixtures.
- [x] Replay six fixtures / ten steps with frozen host reference transitions,
  run public failures, build actual auto/oracle packets, and measure unique-line
  intersection, oracle coverage and source/packet bytes. Validate gold public tests.
- [x] Report abstentions without assigning unmeasured provider costs to zero.

## Reduced proposal after local evidence

- [x] Prefer tasks where v2 acts; avoid paid duplicate control/auto arms when
  local proof establishes identical prompts. Explain omitted diversity strata.
- [x] Enumerate exact reflected sequences (ABBA/BAAB for every comparison), fixed
  repeats and cluster n. Keep old failed attempts/pilot data as version-1 evidence.
- [x] Estimate costs without assuming v2 savings: use the highest v1 arm rate,
  weekly pooled coefficient upper endpoint, 2x reserve, and large-input stress.
  Forecast reserve must be <=10 Codex weekly points and Claude <=$15.
- [x] Start no earlier than both native weekly resets on 2026-10-09: Codex's
  last recorded reset is 21:34:35 UTC, later than Claude's ~21:00 UTC. Require
  fresh readings; this is a plan, not an automatic scheduled run.
- [x] Keep >85% weekly / primary guards, no retries/replacement or automatic
  reset-spanning resume. Explicit approval is needed before any model generation.
- [x] Record that b13628d exists on the main line; rebase onto it only at merge
  time. Do not touch the shared checkout or rewrite this branch now.

## Finish

- [x] Update preregistration and evaluation with v2 local results and reduced budget.
- [x] Run pytest, Ruff check, Ruff format check, mypy and mod tests. No Swift change.
- [x] Commit only named owned files with repository-config noreply email.
- [x] Show the new results/budget and ask approval; stop without generation.

## Reviewed outcome

V2 emits on 4/10 steps, abstains on 6/10, and all six empty prompts equal
control. Two emitted packets have zero oracle overlap; omit those tasks from
paid repetition. The proposed T6-only three-arm reflected study has 12 episodes,
48 maximum invocations, one task cluster and no efficacy CI. With 2x carried
context growth and a further 2x reserve: Claude $13.8708 / $15; Codex weekly
upper-coefficient pooled proxy 9.7544 / 10 points. Earliest start 9 October
21:35 UTC (10 October 00:35 EEST), conditional on actual resets, calibration
and explicit approval. The draft schema is deliberately not executable.
