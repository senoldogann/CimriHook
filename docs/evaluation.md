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
  `model_auto_compact_token_limit`), `rtk` (RTK's Claude Code hook, `rtk hook claude`, which
  rewrites shell commands into their compressed RTK form) and `rtk-governor` (RTK plus the
  window). The harness sets the window through the environment variable so that no settings file
  can change an arm; `cimrihook init` sets the same window through Claude Code's
  `autoCompactWindow` setting. Three arms in the recorded sets belong to mechanisms that were
  measured and then removed from CimriHook (the code is at the git tag `pre-trim`): `brief` (the
  window plus a `PreCompact` hook asking for a short structured summary), `combined` (the window
  plus the tool codec) and `codec` (the codec alone). Their results stay in the tables below.
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

Claude Code 2.1.288, `claude-opus-5-5[1m]`, effort medium, 5 runs per arm, a 2x2 design with
RTK 0.51 at window 233000 (compaction at about 200k tokens), and window 183000 (about 150k)
against the same five baselines (`deeper-183`).

| Arm | Cost (geometric mean) | vs baseline [95% CI] | Runs ok | Steps ok | Mean context | Compactions per run |
|---|---|---|---|---|---|---|
| baseline | $13.15 | - | 5/5 | 100/100 | 350k | 0 |
| window 233000 | $8.03 | x0.610 [0.544-0.685] | 5/5 | 100/100 | 107k | 2 |
| window 183000 | $7.13 | x0.542 [0.480-0.611] | 5/5 | 100/100 | 75k | 3 |
| RTK | $13.50 | x1.026 [0.912-1.154] | 5/5 | 100/100 | 349k | 0 |
| RTK + window 233000 | $7.91 | x0.602 [0.535-0.676] | 5/5 | 100/100 | 105k | 2 |

- Window 233000 cut the provider-billed cost by 39% (95% CI 31.5-45.6%), window 183000 by 46%
  (38.9-52.0%). The 233000 window saved 27% in the `deep` sessions, which peak at 350k: the
  further a session would grow, the more a fixed compaction point saves. With 233000 the
  cumulative ratio is x0.80 after 5 steps, x0.71 after 10 and x0.61 after 20.
- RTK made no measurable difference, alone (x1.026) or on top of the window (RTK + window against
  the window alone: x0.985 [0.935-1.039]). RTK rewrites shell commands; most of these sessions'
  context comes from file reads and the agent's own messages, which it does not touch. Sessions
  dominated by long command output (builds, large test logs) may differ.
- All 500 steps passed in all five arms. Across every arm with a window so far (`deep`,
  `deep-233`, `deeper` and `deeper-183`: 30 runs, 600 steps) no run failed; the one-sided 95%
  upper bound on the run failure rate is 9.5%.

### What a task costs in window points (`limits-deeper`, Claude Code)

On a subscription there is no bill: usage fills a 5-hour and a weekly window, shown in whole
percents. The `meter` and `meter-governor` arms run the `deeper` task without and with window
183000 and load the CimriHook mod only to record the windows after every turn. Claude Code
2.1.289, `claude-opus-5-5[1m]`, effort medium, runs interleaved on one subscription account. Five pairs
ran: six were planned, but the launcher hit its time limit in the sixth, which left no result
files.

| Arm | Cost (geometric mean) | Compactions per run | Points per list-price dollar [95% CI] | 5-hour window per run |
|---|---|---|---|---|
| meter | $12.95 | 0 | 0.176 [0.132-0.219] | 2.3 points |
| meter-governor | $7.08 | 3 | 0.170 [0.111-0.229] | 1.2 points |

- **Raw points mislead.** The runs moved the 5-hour window by 3.8 (meter) and 2.6 (governor)
  points on average, but other sessions on the same account ran meanwhile and spent about as much
  as the runs themselves, and the percentages are whole numbers. The report prints the raw points
  and does not compare them when a run moves a window by less than three points on average.
- **Method.** Between two consecutive whole-percent crossings the window moved by an exact number
  of points. The readings of the runs and of the other recorded sessions are merged, so each
  crossing is bracketed by the readings on either side. Over each span between crossings the
  list-price spend of the meter runs, the governed runs and all other sessions is read off their
  cumulative spend (linear between readings), and the points are regressed on those three spends
  through the origin: 32 spans, 29 degrees of freedom, residual SD 0.22 points
  (`cimrihook.weights`).
- **One point of the 5-hour window was about $4.8 of list-price spend** ($4.38-5.18, one weight for
  every recorded session). The governed weight over the ungoverned one is 0.97 [0.69-1.36]: no
  sign that the window counts a governed dollar differently, though the interval cannot exclude a
  difference of about a third.
- **Window use per task.** The weight ratio times the cost ratio (x0.547 [0.477-0.626]) is x0.53:
  the governed session takes about half the 5-hour window of the ungoverned one, 2.3 against 1.2
  points for this task. The interval of the weight ratio is not carried into this product.
- **The other sessions weigh more per recorded dollar** (0.290 [0.208-0.371]). Use that no session
  records (claude.ai, sessions without the mod) and a different model mix are folded into that
  class. Leaving them out of the fit raises the arms' weights to 0.27 and 0.30, because the arms
  absorb the other use, and keeps the ratio near one (1.13 [0.80-1.60]).
- **The weekly window** moved one point per ungoverned run and 0.2 per governed run: too coarse
  for a fit.

One account and plan, one task, one model, five pairs. Spend is interpolated linearly between
readings, which blurs when it happened and pulls weights toward zero a little; use no session
records raises them. Read the figures as "the saving in dollars carries over to the window"
(roughly, with a wide interval), not as a conversion rate for other plans.

### Mask-first compaction (`deeper`, pilot of one run)

The `mask` arm adds the CimriHook mod with `CIMRIHOOK_MOD_MASK=1` to the same window: automatic
compactions replace older tool results with placeholders instead of an LLM summary. The run is in
`bench/results/deeper-mask`.

| Arm | Runs | Cost | vs window | Steps ok | Requests | Context after the reading phase |
|---|---|---|---|---|---|---|
| window | 5 | $8.03 (geometric mean) | - | 100/100 | 145 (median) | 68-76k (run 2) |
| mask | 1 | $8.85 | x1.10 | 20/20 | 126 | 117-120k |

- Each mask compaction took 2-18 ms; Claude Code's summaries in the window runs took 14-63 s
  (median about 33 s).
- The reading phase cost less with masking ($4.75 against $5.12-5.32 in the window runs), because
  no summary request ran. The run then carried more context out of that phase: its second
  compaction came earlier in the reading, so more file reads followed it. The first resume after
  a mask compaction also missed the prompt cache once (106k tokens written, about $0.85); the
  window runs resume from the cache.
- Earlier pilots (`bench/results/deeper-mask-v0`, `-v1`) are invalid: `--resume` reloaded the
  whole pre-compaction conversation (about 400k tokens per step) until the mod handed back
  rebuilt messages.

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
| boltons deeper, window 183000 | governor | x0.466 | x0.542 [0.480-0.611] | -7.6 pts |
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

## Ideas that did not pay off

Measured before shipping, cheapest test first. None of these is in CimriHook.

- **The tool codec**, which re-encoded a tool result as a reference, a diff or an outline when the
  agent already held the text. Replayed on the author's week it would have saved about 1% of the
  input cost, and no A/B run showed a measurable effect (the `combined` arm in the sequential
  table above). Removed.
- **The compaction brief**, a `PreCompact` hook asking for a short, structured summary that
  refers to code by path and line. A summary is a small part of the context that follows it; the
  `brief` arm was within noise of the plain window (x0.977 against it). Removed.
- **Delegating work to subagents when the context is large.** A prompt note told the agent that a
  step at N tokens costs N/30k times a step in a fresh subagent. Opus 5.5 followed it, one
  subagent per bug, but each step still cost $0.34-0.44 against about $0.37 in the baseline:
  here the large context held exactly the files the subagent then had to read again. Stopped
  after five steps of one `deeper` run.
- **Running the tests after every edit and attaching the result** to spare the request the agent
  spends on running them. Haiku 4.5 and Sonnet 5.5 both ran the tests again anyway, even with a
  note saying they did not need to.
- **A 5-minute cache kept warm by cheap read requests** instead of 1-hour cache writes. Replayed
  on the author's week, it costs 3.9-70% more than today's 1-hour cache: each keep-alive reads
  the whole 200-400k context, while the 1-hour premium only applies to the new tokens.
- **A smaller window for subagents than for the main conversation.** Replaying the author's
  subagent sessions separately puts their best window at the same 150-200k as the main ones.
- **Compressing command output harder.** The author's week put 9.0M tokens of shell output into
  the context, most of it file views and searches the agent asked for. Re-read on every later
  request, that output is about 9% of the cache-read cost, so even perfect compression would
  save 3-5%. RTK's measured effect in the A/B above is in line with that.
- **Mask-first compaction** saved nothing against the window in its pilot; it stays an
  experimental opt-in for its millisecond compactions (above).
- **Shrinking file reads as they enter the context** (an "RTK for the Read tool", which RTK
  cannot touch). On the author's week, with each request's input cost split over what the
  request re-reads by character counts scaled to the measured context, shell output was about
  22% of the input cost, file reads 22%, the agent's own tool inputs (edits, written files,
  commands) 18%, the static prefix 16% and thinking at most 10%; no single source dominates.
  Cutting every unbounded read of a file over 400 lines to 120 lines and an outline, and
  assuming the agent never reads the rest, would have saved under 1%: the large reads were
  few, and the largest "reads" were PDF pages, which are billed by the page, not by their text.
  The first-request allocation above the historical bare baseline estimated 8% of the spend;
  this is not measured recoverable savings. The mod records active context categories.

## What the numbers do not show

- **Quality beyond these tasks.** Bug fixing with a test suite tolerates summarised context well.
  Tasks that depend on details read long ago may suffer more from compaction; the bench measured
  no such task.
- **One model and one effort level** for the deep result (Opus 5.5 with a 1M context, medium).
- **Window points on other plans.** The conversion from list-price dollars to window points
  comes from one account, one task and five pairs; the weights separate the sessions that
  recorded their readings, not claude.ai or sessions without the mod.
- **Idle gaps.** Bench sessions run without pauses, so the prompt cache never expires there: the
  cold-prompt guard is not part of these numbers.
- **Your workload.** The saving depends on how far your sessions grow. `cimrihook doctor` replays
  your own sessions under each window; for the author's last week it predicts about -38% for a
  window that compacts at 200k tokens, with the caveat that the replay has been 0-11 points
  optimistic in every A/B so far.

## Corrections to earlier figures

- Cost is the provider-billed total, which includes the compaction requests. A transcript-only
  cost leaves them out and favours the treatment: a bootstrap over it gave Codex -24% (CI 16-33%)
  and Claude Code -8% (6-11%), where the provider-billed cost of the same runs gives Codex x0.799
  and Claude Code x0.950, with the intervals in the table above.
- "The simulator differs from billing by less than 1.1%" described the replay identity check (the
  observed sessions priced twice), not the prediction error, which is in the calibration table.
- `bench/results/long.log` and `pilot.log` print `reported_usd` as the sum of cumulative totals;
  the per-run cost is the last cumulative value (for example $1.735, not $16.33).
- An early pilot's 40k Claude Code window was silently raised to 100000 by Claude Code; the
  harness now rejects windows below 100000.

## Verified task closure pilot (2026-10-04)

Goal: remove externally verified task work from active context while retaining the cached
shared prefix, original user constraints, corrections and a short result receipt. Hold model
and effort fixed; evaluate cache and resume feasibility before an A/B saving claim.

The full-read pilot ran with Claude Code **2.1.289**, **Opus 5.5 1M / medium** and the existing
**183000 governor window**. The host required a successful full `Read` of the disposable fixture,
checked its first and last markers, ran unchanged Python tests, and archived the original JSONL.

| Gate | Observation | Result |
|---|---|---|
| Warm shared conversation beyond static input | static input 5347; first fix cache read 30000 | Pass |
| First main request after closure retains shared cache | 30000 cached tokens, 100% of first fix's cache read; gap 3.509 seconds | Pass |
| Observation stays removed | absent at first post-close and second resume starts | Pass |
| Fixture and retained contract | external tests pass before/after; both JSON answers retain identifier, value 75 and tests_verified: true using actual tool evidence | Pass |
| Closure generates no summary request | cumulative provider cost unchanged at $0.4735866 during `/compact` | Pass |

The last fix request carried **55732 input tokens**; the first post-close request carried
**31079**, and the second resume **31568**. The observed first reduction is **44.2%**. These
are different consecutive requests, not a paired cost or quota comparison. The full session's
last cumulative API-equivalent cost was **$0.507106**; cumulative totals are not added together.
Projected messages went from 18 to 11; character counts are not treated as tokens.

Raw Claude utilization remained **5% five-hour / 48% weekly** before and after the pilot.
This does not imply zero consumption: the CLI returned whole account-wide percentages.
The real sequential-task comparison below tests cost against the existing governor. This pilot
alone does not establish general quality or subscription savings.

API findings from the attempts leading to the full-read pilot:

- Programmatic `$.session.compact()` is unavailable in `-p`/SDK sessions in this build.
  Calling it from `prompt.submit` is also rejected. The experiment uses the supported
  controlled `/compact` command, with validation and a scoped veto in its compaction hook.
- The engine appends a known command message before manual compaction. Only that exact final
  control message is excluded from the completed-boundary comparison.
- Decoded tool `result` bodies may be cleared after a turn and restored on resume. Comparison
  uses stable visible text, tool identities/inputs and error flags; opaque handles are never
  persisted. Only the original unmodified shared prefix uses current-event handles; previously
  rebuilt task receipts are reconstructed without handles.
- An earlier successful run sampled the observation and reduced context only about 8%.
  The final driver rejects sampling and requires a full successful `Read` before closure.
- The first full-read run removed all task tool evidence. It preserved the number and
  identifier, but the assistant explicitly rejected the receipt's verification provenance.
  This revealed a quality defect the original recall predicate missed. The final implementation
  retains actual successful Edit and unittest tool-use/result pairs without old handles;
  it requires both continuations to use that test evidence. The earlier
  [report without tool proof](../bench/results/task-closure-pilot-20261004/report-without-tool-proof.json)
  is marked unsuccessful under the stronger contract gate.
- Rejected proof, changed prefix/files, archive/receipt-write errors and hook exceptions veto
  compaction without invoking downstream summarization. Normal usage does not enable closure.

The quota command independently reads Claude `get_usage` and Codex `account/rateLimits/read`.
Protocol normalization was checked against T3 Code commit
[`4ee6bfd`](https://github.com/pingdotgg/t3code/tree/4ee6bfd50ef4a089440d5c3662db2298da9cc50e):
[Claude quota reader](https://github.com/pingdotgg/t3code/blob/4ee6bfd50ef4a089440d5c3662db2298da9cc50e/apps/server/src/provider/Layers/claudeUsageLimits.ts),
[Codex quota reader](https://github.com/pingdotgg/t3code/blob/4ee6bfd50ef4a089440d5c3662db2298da9cc50e/apps/server/src/provider/Layers/codexUsageLimits.ts),
[Usage documentation](https://github.com/pingdotgg/t3code/blob/4ee6bfd50ef4a089440d5c3662db2298da9cc50e/docs/user/usage.md).
Usage pricing and actual allowance observations are separate in T3 and in CimriHook.

[Provider measurements](../bench/results/task-closure-pilot-20261004/report.json) include
first-request usage, raw quota snapshots and cumulative cost. The full original transcript,
projection archive, fixture and logs are retained privately under
`~/.cimrihook/experiments/task-closure-20261004-with-proof/`. The public report contains the isolated
fixture's answers and measurements, without credentials.

```bash
cimrihook closure-probe --output /tmp/cimrihook-closure-new
cimrihook quota --agent claude
cimrihook quota --agent codex
uv run python tests/run_mod_tests.py
```

## Verified closure on real sequential tasks (2026-10-04)

Claude Code **2.1.289**, **Opus 5.5 1M / medium**, window **183000**, Boltons **26.2.0**
(`4332b35a278d694f30c99881faa61cde695c7a96`). Each session fixes the same 20 injected bugs
in order. There is no synthetic observation or forced repository warmup. Both arms use the
same private measurement plugin, tools (Read/Edit/Bash/Glob/Grep), settings and environment;
closure is the only treatment. The ordinary sessions stay below the governor's automatic
compaction threshold, so this comparison concerns shorter sessions.

The host verifies that each injected bug fails tests, seals its history, and runs the full
original test suite after every fix. All five measured sessions passed **20/20 fixes** with
**519 original tests** per fix and no changed test files. All library Python files were
restored **byte for byte** to the reference repository in every session. Successful native
Edit and pytest records and all original user prompts are retained at closure. There was no
extra Read repetition in any arm; tool counts are in the report.

| Arm | First session, API-equivalent USD | Reverse-order repeat, USD | Fixes passed |
|---|---|---|---|
| Existing governor | $1.227217 | $1.1624804 | 40/40 |
| Governor + closure every five verified tasks | $1.1684188 | $1.1241478 | 40/40 |
| Governor + closure after every verified task | $2.4834028 | Not repeated | 20/20 |

The five-task treatment closes after tasks 5, 10 and 15. Closing the final task would have no
subsequent request to save. The paired order is governor then treatment, followed by treatment
then governor. All closures made **zero summary model requests** and left the cumulative
provider cost unchanged during the control command.

### Cost and cache result

- **Closing every task costs more.** Against the first governor it costs **102.4% more**,
  even though total processed input falls **45.7%**. Newly written cache tokens grow from
  **50,889 to 241,633** (4.75 times). Cache-read cost falls from $0.5161 to $0.2372, but
  cache-write cost rises from $0.4071 to $1.9331. Removing context did not repay the rewrites.
- **Five-task closure has a small observed benefit.** Costs fall **4.79%** and **3.30%** in
  the two pairs; the geometric mean saving is **4.05%**. The 95% paired log-cost t interval
  is **x0.869–1.059** (two run pairs, one degree of freedom). It includes no saving and
  a cost increase. This is a candidate for more evidence, not an established population benefit.
- **Use full provider counters.** Per-request input counters and native cache TTL splits,
  combined with full `turn.step` output counters, reproduce every reported final cumulative cost
  exactly at the published model prices. SDK transcript output counters underreport the full
  output; their difference is not treated as an exact reasoning-token count.
- **Subscription saving remains unknown.** The quota snapshots are account-wide integers;
  a Fable-specific window also advanced during Opus-only runs. Concurrent or delayed account
  use and coarse observations prevent attribution. No percentage of extra subscription work
  is inferred from the API-equivalent saving. Elapsed time includes tests and CLI startup,
  and is reported separately from cost.

### Reliability and decision

Repeated closure exposed a resume defect: reusing handles for already reconstructed task
records duplicated their tool IDs and messages on subsequent resumes. Only the untouched
original prefix now retains handles; previous receipts are rebuilt without them. The benchmark
rejects duplicate native tool IDs. The failed setup attempts are archived and explicitly
excluded from the comparison, with their consumed provider costs recorded.

The repeated five-task experiment remains isolated and explicitly enabled. **Do not enable
per-task automatic closure.** Keep the existing governor as the product default. There is no
dynamic window controller, task classifier, centralized telemetry or new product command.
Quality here covers these test-based bug fixes; architectural work, dependent refactors and
automatic compaction occurring within a pending task group have not been evaluated.

[Measurements, raw quota snapshots, cost decomposition and excluded attempts](../bench/results/closure-ab-20261004/report.json).
Full transcripts, workspaces, verification records and closure archives are retained privately
in `~/.cimrihook/experiments/closure-ab-20261004/`. The plan review and priorities are in
[development-review.md](development-review.md).

To reproduce an arm with a new private directory:

```bash
uv run python bench/closure_ab.py --arm governor --close-every 5 \
  --output /tmp/cimrihook-closure-governor-new \
  --task bench/tasks/boltons-twenty-steps.json --model 'claude-opus-5-5[1m]' \
  --effort medium --window 183000 --timeout 300 --steps 20 --tools Read,Edit,Bash,Glob,Grep
uv run python bench/closure_ab.py --arm closure --close-every 5 \
  --output /tmp/cimrihook-closure-batch-new \
  --task bench/tasks/boltons-twenty-steps.json --model 'claude-opus-5-5[1m]' \
  --effort medium --window 183000 --timeout 300 --steps 20 --tools Read,Edit,Bash,Glob,Grep
```

`--close-every 1` reproduces the rejected per-task treatment. This cadence is an experiment
parameter, not an automatic policy or the Anthropic Batch API.

## Focused tool profile on real sequential tasks (2026-10-04)

The next prefix experiment reused the sequential-task driver and existing governor. The first
inspection found an active 29469-token MCP category in only **one of 19** recorded sessions;
deferred tools are excluded. `doctor` now shows the number of sessions containing each category
and labels its median as conditional on presence. A large rare category is not every session's
prefix. Claude Code already defers MCP tools by default; this experiment tests the built-in
tool profile, with no MCP servers connected. [Claude Code tool search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search).

**Method.** Claude Code 2.1.289, `claude-opus-5-5[1m]`, medium effort, 183000 window, the same
Boltons 26.2.0 reference and injected bugs as above. Both arms use the same private plugin,
allowlisted environment, project settings only, empty strict MCP config, and automatic
permissions for Read/Edit/Bash/Glob/Grep. Agent is denied in both. The changed parameter is
`--tools default` versus `--tools Read,Edit,Bash,Glob,Grep`. The recorded default inventory has
26 tools, including Glob/Grep; the focused inventory has five. Removing Skill also removes
its skill listing, so this is a complete profile comparison, not just tool-schema bytes.

Three-task screening ran default then focused. The default's first request was cold, while
focused reused 4541 cached tokens. Its apparent 52% cost reduction is **not a balanced saving
measurement**. A separate 20-task pair ran in the reverse order, focused then default, after
both profiles had recently been used. Screening and confirmation are not pooled.

| 20-task confirmation | Default profile | Five-tool profile |
|---|---:|---:|
| Host-verified fixes | 20/20 | 20/20 |
| Tests per step; test files unchanged | 519; yes | 519; yes |
| Final Python library files identical to reference | 30/30 | 30/30 |
| Final active-category snapshot, excluding messages | 13332 tokens | 7016 tokens |
| First-request input, including initial messages | 16902 tokens | 8048 tokens |
| Main model requests | 83 | 82 |
| Processed input, including cache reads | 3150028 tokens | 2510563 tokens |
| New cache writes | 49201 tokens | 51437 tokens |
| Generated output, including thinking | 12010 tokens | 13432 tokens |
| Cumulative API-equivalent cost | $1.2546042 | $1.1725844 |
| Whole pipeline time, including host tests/startup | 468.0 s | 369.0 s |

The observed cost reduction is **6.54%**; processed input falls **20.30%** and the final active
snapshot **47.37%**. Excluding the first task from both arms still gives **5.45%** lower
incremental cost. Cache-read cost falls from $0.6201322 to $0.4917924, while cache-write cost
rises from $0.393608 to $0.411496 and output cost from $0.2402 to $0.26864. Smaller input does
not predict the net saving by itself. Provider counters, native transcript counters and the
CLI cumulative cost agree; all writes use the one-hour TTL. The full screening and confirmation
have a combined API-equivalent cost of $2.816986; the runs authenticated with the subscription.
[Opus pricing](https://platform.claude.com/docs/en/about-claude/pricing).

**Limits and decision.** This is one paired session in one repository/model/effort. Twenty
dependent tasks are not twenty independent replicates; there is no population confidence
interval or general quality guarantee. Native tool use remained within the five-tool profile
in both arms. The governor did not trigger, and larger architectural tasks or tasks needing
omitted tools were not evaluated. The shorter elapsed time is an observation including
network/startup/test variation, not an isolated latency estimate.

The five-hour account readings were 25→27 for focused and 27→30 for default; weekly readings
were 52→52 and 52→53. These are account-wide rounded values during concurrent activity.
They do not establish attributable subscription savings or a tasks-per-allowance multiplier.
The existing governor benchmark already used the five-tool profile; this percentage cannot
be added to its previous savings.

Use the existing explicit tool flag for known tasks that fit this profile. Keep it opt-in;
there is no measured reason to add a classifier, dynamic profile switching or a new product
command. Global user settings are unchanged.
[Measurements and quota snapshots](../bench/results/prefix-ab-20261004/report.json).
Full workspaces, prompts, transcripts, the independent source/counter audit and the summary
script remain private in `~/.cimrihook/experiments/prefix-ab-20261004/`.

To reproduce a 20-task arm with a new private directory:

```bash
uv run python bench/closure_ab.py --arm governor --close-every 5 \
  --output /tmp/cimrihook-prefix-focused-new \
  --task bench/tasks/boltons-twenty-steps.json --model 'claude-opus-5-5[1m]' \
  --effort medium --window 183000 --timeout 300 --steps 20 --tools Read,Edit,Bash,Glob,Grep
```

Use `--tools default` and another new directory for the control; `--steps 3` selects screening.
No task closure occurs in the governor arm.

## Explicit local task preparation screening (2026-10-05)

The opt-in preparation prototype preserves the original request and attaches literal source
selected by explicitly declared file/range/Python-symbol targets. Source content is not summarized
by a model. The opportunity report and packet builder make no model calls.

The screening uses More-itertools v11.1.0 and one injected `exactly_n` bug. Both arms get the same
explicit `exactly_n` / `ExactlyNTests` scope and the same host-observed failing test tail. Only the
prepared arm gets literal source excerpts and Git/hash metadata. Targets are declared independently
in [the task fixture](../bench/tasks/preparation-smoke.json); the builder never searches the mutation
replacement. Both use the 183000 governor window and medium effort. Claude Code 2.1.289 uses
`claude-opus-5-5[1m]`; Codex 0.160.0 uses `gpt-6.1-sol`. Calls run serially in the order Claude
control, Claude prepared, Codex control, Codex prepared. This is one pair per agent, not a
counterbalanced or replicated estimate.

| Agent / arm | Provider cost or fixed price-table units | Requests | Generated output | Host acceptance |
|---|---:|---:|---:|---|
| Claude targeted governor | $0.090335 | 4 | 739 tokens | Pass |
| Claude prepared governor | $0.0851292 | 2 | 596 tokens | Pass |
| Codex targeted governor | 33397.8 base input units | 5 | 760 tokens | Pass |
| Codex prepared governor | 25784.0 base input units | 5 | 518 tokens | Pass |

Every workspace passed **722 tests and 19896 subtests**, with unchanged test files and the target
library file byte-identical to the pinned reference after fixing. A separate host rerun confirmed
each result. There were four attempted runs and zero failed/unmeasured attempts. Claude's combined
reported cost is $0.1754642 at API list prices; calls authenticated with the subscription.

Observed cost ratios are Claude **x0.9424** (5.76% lower) and Codex **x0.7720** (22.80% lower).
Codex units use the existing benchmark table: uncached input 1, cached input 0.1, output 6;
they are not dollars, current model-specific credit prices or a subscription bill. Raw counters
remain in [the four result records](../bench/results/preparation-screen-20261005/). No confidence
interval or general quality non-inferiority is established from one pair. No compaction occurred.

Codex's first/last native five-hour readings moved 12→14 in control and 14→15 in preparation;
both weekly readings stayed at 56. The first reading is already after a request, these values are
account-wide rounded observations, and concurrent work was running. They do not establish
attributable quota savings. Claude quota use was not measured in this screening.

The local seven-day opportunity snapshot found 4921 recognized research calls and 2034
research-only Claude request batches before the first edit, with 194 identical successful
observations. These are candidate preparation opportunities, not measured avoidable work.
Codex's wrapper-heavy logs classified only 18 of 4521 native calls as research; the report leaves
the other 4503 calls visible as other/unclassified instead of interpreting arbitrary wrapper code.
Recently modified logs can include older history; prompt spans are not completed tasks.

Decision: retain explicit local preparation as experimental. A counterbalanced repeat across
additional tasks, dependent-refactor acceptance, and usable quota resolution are required before
claiming more accepted work per subscription. No dynamic controller, automatic profile selection,
or installed-setting change is part of this screening. Exact prompts, native outputs, independent
host verification and aggregate opportunity JSON are retained locally in
`/tmp/cimrihook-bench/preparation-screen-20261005/`.

## Preregistered preparation study: measurement pilot approved

The [preparation A/B preregistration](preparation-ab-preregistration.md) freezes an
exploratory six-task, three-repository design with control/oracle/auto/wrong arms,
two repeats per arm and agent (96 episodes, at most 160 generation invocations).
Its primary comparison is deterministic no-model auto preparation versus governor
without a packet. The [implementation plan](superpowers/plans/2026-10-05-preparation-ab.md)
keeps new code beside the existing benchmark runner.

**Current status: pilot approved; infrastructure locally verified before generation.
Zero new model benchmark invocations at the execution freeze.** The approved
measurement pilot is one episode per arm per agent, eight total, n=1 task and no efficacy CI.
The full study requires separate approval after that pilot. Six fixtures / ten steps pass no-model eligibility. No agent quality or
repeatability result is established. Full-run CIs will bootstrap six task clusters;
shared repository provenance and the small sample limit generalization.

The stage-1 estimate uses historical control consumption for every arm, without
assuming savings. Pilot expected/reserved consumption is Claude $0.3613/$0.7227,
Codex 1.7659/3.5318 predicted five-hour points and 0.3020/0.6040 weekly pooled proxy
points. Full-study estimates, excluding pilot, are $7.2268/$14.4536,
35.3185/70.6369 five-hour points and 6.0402/12.0804 weekly proxy points.
The reserve is 2×, not a bound. Separate weekly input/output weights are
unidentifiable; the pooled estimate's coefficient-only 95% interval is documented
in the preregistration. No Claude window-point conversion is supported by these USD.
Every relevant weekly window exceeding 85% stops active and queued generation.

## Reproduce existing A/B studies

```bash
cimrihook bench-run --name deep --tasks boltons-twenty-steps --protocols deep --agents claude \
  --variants baseline,governor --window 183000 --reps 5 --concurrency 3
cimrihook bench-report --name deep
cimrihook bench-calibrate --name deep
```

The 233000 set (`deep-233`) reuses the five baseline results of `deep` and adds the governor arm
with `--window 233000`; `deeper-183` reuses the baselines of `deeper` the same way with
`--window 183000`. The 2x2 set needs RTK on the `PATH`:

```bash
cimrihook bench-run --name deeper --tasks boltons-twenty-steps --protocols deeper --agents claude \
  --variants baseline,governor,rtk,rtk-governor --window 233000 --reps 5 --concurrency 3
```

Every result file in `bench/results/<set>/` keeps the run's model, effort, window, agent version
and per-step costs; `cimrihook bench-remeasure` recomputes a set from the agents' logs.

The window-points set needs a subscription and an otherwise quiet account; the readings of
your other sessions come from the mod's ledger (`~/.cimrihook/limits`) unless you point
`--background` at a directory of `*.jsonl` files:

```bash
cimrihook bench-run --name limits-deeper --tasks boltons-twenty-steps --protocols deeper \
  --agents claude --variants meter,meter-governor --window 183000 --reps 6 --concurrency 1
cimrihook bench-report --name limits-deeper
```

`bench/results/limits-deeper/background/` holds the slice of the author's ledger for the
hours of the runs (sessions renamed `other-NN`); `cimrihook bench-report --name limits-deeper
--background bench/results/limits-deeper/background` reproduces
`bench/results/limits-deeper.report.txt`.
