# Task Closure Pilot Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task.

**Goal:** Read real subscription windows and verify task closure on the installed Claude engine.

**Architecture:** Keep existing USD/session ledger intact. Add bounded standard-library
CLI control clients, an opt-in closure module, and an isolated fixture pilot. Keep all
provider requests at the same model and effort. Record negative results explicitly.

**Tech Stack:** Python 3.12 standard library, Claude function hooks TypeScript, pytest/ruff/mypy.

## Task 1: Protocol and quota observations

- [ ] Add `src/cimrihook/quota.py`: immutable snapshot/window models, Claude/Codex
  response normalizers, bounded subprocess line RPC clients, explicit errors and cleanup.
- [ ] Add `quota --agent` command to `src/cimrihook/cli.py`; JSON output is structured
  observation, not session cost. No auth file reads, model prompts or synthetic USD.
- [ ] Verify transforms for fractions versus percentages, unsupported, scoped Codex
  bucket and missing windows; verify the actual read-only protocols on both CLIs.

## Task 2: Compaction state and closure module

- [ ] Fix pending/succeeded/skip/error state in `src/cimrihook/mod/register.ts`.
- [ ] Add `src/cimrihook/mod/closure.ts`; gate by `CIMRIHOOK_MOD_CLOSE_PROBE=1`.
- [ ] Capture immutable anchor only for armed fix turn; process once-only closure
  before any capture. Bind request to completed projection and fixture contents.
  Persist canonical task anchor, reject mismatched prefix or unverified closure,
  archive original messages before replacement, retain all task user messages and
  final answer, and record counts/characters and engine outcomes. Rejection/archive
  failure and scoped hook catch return skip veto, never downstream summary.
- [ ] Include new module in `mod/hooks.json` and `mods.py` package installation layout.
- [ ] Run `claude plugin validate` against the packaged plugin and focused engine tests.

## Task 3: Isolated provider pilot

- [ ] Add `src/cimrihook/closure_probe.py` and `closure-probe` CLI arguments for model,
  effort, timeout and output directory. Use a private fixture workspace with fixed
  test commands, known retained contract and a disposable large observation.
- [ ] Run prefix warm-up, fix task, external test/file validation, controlled closure,
  follow-up and a second resume. Capture per-turn token usage and cumulative provider
  cost without double-counting.
- [ ] Gate cache on first post-close main request: >=95% of first fix's cache read,
  with fix read >= initial static input +8192 and <=300s latest warm gap.
  Record second resume's observation absence, fixture/contract checks
  and account-wide raw quota observations separately. Do not state task quota savings.

## Task 4: Required verification and report

- [ ] Run meaningful protocol/pilot record tests plus `uv run ruff check`,
  `uv run mypy`, `uv run pytest`.
- [ ] Run real quota reads and one bounded closure pilot.
- [ ] Document current experimental status in README and evaluation, including T3
  source commit and the limitation of coarse/account-wide quota observations.
- [ ] Inspect diff/status and report exact passing/negative gates. No automatic install
  into user settings, publication, or broad A/B batch in this first experiment.
