# CimriHook evaluation

This page reports what CimriHook's mechanisms did to the cost and the success of real coding
sessions, measured with the A/B harness in `cimrihook bench-run` on the author's own Claude Code
and ChatGPT subscriptions. It replaces earlier figures that left the compaction requests out of the
cost (see [Corrections](#corrections-to-earlier-figures)).

## How it was measured

- **Tasks.** Real Python libraries at a pinned tag (`boltons` 26.2.0, `more-itertools`) with
  one-line bugs injected into the library code. A step succeeds when the repository's own test
  suite passes and no test file was touched; a run succeeds when every step does.
- **Protocols.** `sequential`: 20 bugs arrive one at a time in the same session, so the context
  accumulates the way it does in real work (peaks of 85-125k tokens). `deep`: before the first bug
  the agent reads every library source file (about 215k tokens of mostly stale context), then
  fixes the same 20 bugs; the baseline session peaks at about 350k tokens. `deeper`: the agent
  also reads every test file first, so the baseline session peaks at about 450k tokens. That is the
  regime where most real spend happens: in the author's last week of Claude Code use, 57% of the
  spend went to requests carrying more than 400k tokens.
- **Arms.** `baseline` (default behaviour), `governor` (Claude Code
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, which compacts at the window minus 33k tokens; Codex
  `model_auto_compact_token_limit`), `brief` (the window plus the `PreCompact` brief), `combined`
  (the window plus the tool codec), `rtk` (RTK's Claude Code hook, `rtk hook claude`, which rewrites
  shell commands into their compressed RTK form) and `rtk-governor` (RTK plus the window). The
  harness sets the window through the environment variable so that no settings file can change an
  arm; `cimrihook init` sets the same window through Claude Code's `autoCompactWindow` setting.
- **Isolation.** Every run gets its own workspace and virtual environment, an allowlisted
  environment, project settings only and `--strict-mcp-config` (Claude Code) or
  `--ignore-user-config` (Codex). The arms of a result set share model, effort and agent version.
- **Primary cost.** What the provider bills, compaction requests included: Claude Code's cumulative
  `total_cost_usd` (list-price USD) and, for Codex, the rollout's per-response usage records priced
  with an explicit sheet. The transcript-only cost, which leaves compaction out, is reported
  alongside.
- **Statistics.** Costs are compared as ratios of geometric means with a 95% Welch t interval on
  log cost (exact quantile for fractional degrees of freedom). Pooled results show two intervals:
  across scenarios (generalises beyond them) and within the measured scenarios (Satterthwaite).
  Success is compared per run with Newcombe's interval; a step-level comparison would treat the
  20 dependent steps of a run as independent and is shown for description only.

## Results

### High-context sessions (`deep`, Claude Code)

Claude Code 2.1.288, `claude-opus-5-5[1m]`, effort medium, 5 runs per arm, window 183000
(compaction at about 150k tokens).

| Arm | Cost (geometric mean) | vs baseline [95% CI] | Runs ok | Steps ok | Mean context | Compactions per run |
|---|---|---|---|---|---|---|
| baseline | $9.22 | - | 5/5 | 100/100 | 273k | 0 |
| governor | $5.51 | x0.598 [0.581-0.616] | 5/5 | 100/100 | 79k | 2 |
| brief | $5.38 | x0.584 [0.571-0.597] | 5/5 | 100/100 | 77k | 2 |

- The window cut the provider-billed cost by 40% (95% CI 38-42%). The saving grows along the
  session: the cumulative ratio is x0.78 after 5 steps, x0.69 after 10 and x0.60 after 20.
- The brief made no measurable difference on top of the window: brief vs governor x0.977
  [0.950-1.004].
- No run and no step failed in any arm. With zero failures in 15 treated runs (both windows and
  the brief), the one-sided 95% upper bound on the run failure rate is 18%; showing
  non-inferiority at -5 points per run would take about 75 runs per arm.

### Sessions that grow to 450k tokens: the window against RTK (`deeper`, Claude Code)

Claude Code 2.1.288, `claude-opus-5-5[1m]`, effort medium, 5 runs per arm, window 233000
(compaction at about 200k tokens), a 2x2 design with RTK 0.51.

| Arm | Cost (geometric mean) | vs baseline [95% CI] | Runs ok | Steps ok | Mean context | Compactions per run |
|---|---|---|---|---|---|---|
| baseline | $13.15 | - | 5/5 | 100/100 | 350k | 0 |
| window | $8.03 | x0.610 [0.544-0.685] | 5/5 | 100/100 | 107k | 2 |
| RTK | $13.50 | x1.026 [0.912-1.154] | 5/5 | 100/100 | 349k | 0 |
| RTK + window | $7.91 | x0.602 [0.535-0.676] | 5/5 | 100/100 | 105k | 2 |

- The window cut the provider-billed cost by 39% (95% CI 31.5-45.6%). The same window saved 27% in the
  `deep` sessions, which peak at 350k: the further a session would grow, the more a fixed
  compaction point saves. The cumulative ratio is x0.80 after 5 steps, x0.71 after 10 and x0.61
  after 20.
- RTK made no measurable difference, alone (x1.026) or on top of the window (RTK + window against
  the window alone: x0.985 [0.935-1.039]). RTK rewrites shell commands; most of these sessions'
  context comes from file reads and the agent's own messages, which it does not touch. Sessions
  dominated by long command output (builds, large test logs) may differ.
- All 400 steps passed in all four arms. Across every arm with a window so far (`deep`,
  `deep-233` and `deeper`: 25 runs, 500 steps) no run failed; the one-sided 95% upper bound on
  the run failure rate is 11%.

### The window `doctor` recommends (`deep`, Claude Code)

Same scenario and baselines, window 233000 (compaction at about 200k tokens), 5 runs.

| Window | Compaction at | vs baseline [95% CI] | Runs ok | Steps ok | Mean context | Compactions per run |
|---|---|---|---|---|---|---|
| 183000 | about 150k | x0.598 [0.581-0.616] | 5/5 | 100/100 | 79k | 2 |
| 233000 | about 200k | x0.734 [0.714-0.755] | 5/5 | 100/100 | 141k | 1 |

In this scenario the smaller window saves clearly more: these sessions peak at 350k tokens, so a
200k trigger keeps much of the stale context. On the author's real sessions, which grow further and
whose compaction summaries are longer (about 7k tokens against 1.5k here), the replay finds the
two windows equal (-38.1% at 200k, -38.3% at 150k) while the 200k trigger needs half the
compactions (384 against 707 a week). `doctor` therefore recommends the larger window; both
windows passed every step.

### Long sequential sessions (moderate context)

`sequential` protocol, 2 runs per arm and scenario (`boltons`, `more-itertools`), window 100000
for Claude Code (compaction at about 67k) and 60000 for Codex.

| Agent | Arm | vs baseline | 95% CI, these scenarios | 95% CI, across scenarios | Runs ok |
|---|---|---|---|---|---|
| Claude Code | window + codec | x0.950 | 0.892-1.012 | 0.504-1.790 | 4/4 vs 4/4 |
| Codex CLI | window | x0.799 | 0.522-1.224 | 0.362-1.763 | 4/4 vs 4/4 |

At these context sizes the effect is small for Claude Code and uncertain for Codex. Both intervals
include 1.

### Simulator calibration

`cimrihook bench-calibrate` replays the baseline runs at the treatment's real trigger point. The
compaction parameters come from the treatment runs themselves (in-sample), so this checks the cost
accounting, not out-of-sample prediction.

| Scenario | Arm | Predicted | Measured [95% CI] | Error |
|---|---|---|---|---|
| boltons deep | governor | x0.575 | x0.598 [0.581-0.616] | -2.3 pts |
| boltons deep | brief | x0.539 | x0.584 [0.571-0.597] | -4.5 pts |
| boltons deep, window 233000 | governor | x0.691 | x0.734 [0.714-0.755] | -4.2 pts |
| boltons deeper, window 233000 | governor | x0.502 | x0.610 [0.544-0.685] | -10.9 pts |
| boltons deeper, window 233000 | RTK + window | x0.510 | x0.602 [0.535-0.676] | -9.2 pts |
| boltons sequential (Claude) | combined | x0.955 | x0.998 [0.875-1.140] | -4.4 pts |
| more-itertools sequential (Claude) | combined | x0.904 | x0.904 [0.666-1.226] | 0.0 pts |
| boltons sequential (Codex) | governor | x0.691 | x0.751 [0.553-1.019] | -6.0 pts |
| more-itertools sequential (Codex) | governor | x0.780 | x0.851 [0.321-2.258] | -7.1 pts |

Every error is on the optimistic side, and in the deep and deeper runs the prediction lies below
the measured interval. The replay did rank the two deep windows correctly. The gap fits the agent
re-reading files after a compaction, which the replay cannot see: 0-13k tokens per compaction in
the deep runs, about 7k at the median, so `doctor` and `simulate` add 7,000 re-read tokens to every
simulated compaction. In `deeper` the gap is larger (-10.9 points): there every bug lies in a file
the baseline session already holds from its first reading, while after a compaction the agent
reads it again at every step. The more of the dropped context a session needs later, the more
optimistic the replay.

## What the numbers do not show

- **Quality beyond these tasks.** Bug fixing with a test suite tolerates summarised context well.
  Tasks that depend on details read long ago may suffer more from compaction; the bench measured
  no such task.
- **One model and one effort level** for the deep result (Opus 5.5 with a 1M context, medium).
- **Idle gaps.** Bench sessions run without pauses, so the prompt cache never expires there: the
  cold-prompt guard is not part of these numbers.
- **Your workload.** The saving depends on how far your sessions grow. `cimrihook doctor` replays
  your own sessions under each window; for the author's last week it predicts about -38% for a
  window that compacts at 200k tokens, with the caveat that the replay has been 0-11 points
  optimistic in every A/B so far.

## Corrections to earlier figures

- The thesis proposal (`docs/opinnaytetyo-ehdotus.md`) cites Codex -24% (CI 16-33%) and Claude
  Code -8% (6-11%). Those came from a bootstrap over the transcript-only cost, which leaves the
  compaction requests out and favours the treatment. On the provider-billed cost the same runs give
  Codex x0.799 and Claude Code x0.950, with the intervals in the table above.
- "The simulator differs from billing by less than 1.1%" described the replay identity check (the
  observed sessions priced twice), not the prediction error, which is in the calibration table.
- `bench/results/long.log` and `pilot.log` print `reported_usd` as the sum of cumulative totals;
  the per-run cost is the last cumulative value (for example $1.735, not $16.33).
- An early pilot's 40k Claude Code window was silently raised to 100000 by Claude Code; the
  harness now rejects windows below 100000.

## Reproduce

```bash
cimrihook bench-run --name deep --tasks boltons-twenty-steps --protocols deep --agents claude \
  --variants baseline,governor,brief --window 183000 --reps 5 --concurrency 3
cimrihook bench-report --name deep
cimrihook bench-calibrate --name deep
```

The 233000 set (`deep-233`) reuses the five baseline results of `deep` and adds the governor arm
with `--window 233000`. The 2x2 set needs RTK on the `PATH`:

```bash
cimrihook bench-run --name deeper --tasks boltons-twenty-steps --protocols deeper --agents claude \
  --variants baseline,governor,rtk,rtk-governor --window 233000 --reps 5 --concurrency 3
```

Every result file in `bench/results/<set>/` keeps the run's model, effort, window, agent version
and per-step costs; `cimrihook bench-remeasure` recomputes a set from the agents' logs.
