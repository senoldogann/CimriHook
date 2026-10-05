# Preparation benchmark preregistration

Status: **stage 1, awaiting explicit approval; no new agent runs authorized or started.**
Registered on 2026-10-05 against CimriHook `0a13163`. The design commit freezes this
document. The [implementation plan](superpowers/plans/2026-10-05-preparation-ab.md)
describes work after approval. The earlier screening is historical budget evidence,
not part of this study's sample.

## Approval boundaries

1. Stage 1 contains source inspection, this design, budget estimation and local
   repository verification. No pilot, agent generation or benchmark calibration run.
2. First explicit approval authorizes infrastructure, fixture eligibility checks
   without agents, and **eight pilot episodes: one per arm per agent**. Stop after
   the pilot and present measurements, problems and a revised full-run budget.
3. A second explicit approval authorizes the full study. Neither approval authorizes
   push, PR creation or tags. No automatic expansion, replacement runs or retries.

Any fixture adjustment discovered by local eligibility checks must be recorded in
a preregistration amendment and committed before the first pilot generation. A
change to tasks, sample size, arm behavior or consumption allowance requires renewed
approval. Corrections cannot depend on observed model performance. CLI examples in
the implementation plan are proposed interfaces, not commands to run in stage 1.

## Questions and hypotheses

The practical objective is more accepted work at the same quality per subscription
allowance. A cheaper failed fix is not evidence of meeting that objective.

| Uncertainty | Prespecified comparison and evidence |
|---|---|
| Repeatability | Paired `auto-control` and `oracle-control` episode costs across six tasks, two repeats, both agents; order and cache counters retained. |
| Realistic target selection | `auto-control` is primary; `oracle-auto` measures the gap to externally known targets. Record selections, evidence origins, empty selections and target coverage after the run. |
| Incorrect targets | `wrong-control` costs, acceptance difference, packet bytes and time spent investigating wrong targets where native logs support classification. Unknown tool classifications stay unknown. |
| Task diversity | Six tasks across three pinned repositories, including multifile, mixed target forms, changed source and dependent steps. Per-task outcomes are mandatory. |
| Subscription relevance | Codex measured five-hour point predictor, fixed API-equivalent units and output-weight sensitivity; weekly pooled proxy separately labeled. Claude provider-reported USD primary, observed quota deltas only when attributable. |
| Quality | Public suite, test integrity, hidden acceptance, reference agreement and an overall accepted flag for every episode and step; failures retained. |

**H1 (primary):** `auto` decreases episode cost against `control` without reducing
host acceptance on these tasks. Primary cost is Claude provider-reported USD and
Codex predicted five-hour points using frozen measured coefficients. Agents are
analyzed separately. **H2:** `oracle` reduces cost, estimating the opportunity when
targets are known. **H3:** `wrong` can increase cost or reduce acceptance. H2/H3,
task-type slices and sensitivity analyses are exploratory.

No population-level quality non-inferiority claim is possible with six tasks. A
favorable cost interval plus no additional observed failures supports a candidate
for a larger study, not established subscription savings. Existing screening's
Claude −5.8% and Codex −22.8% do not supply an expected effect or a power calculation.

## Four arms and common treatment

| Arm | Target policy | Agent-visible preparation |
|---|---|---|
| `control` | No targets selected | Original task/current step and public failing-test output under the common evidence policy; governor, no source packet. |
| `oracle` | Frozen correct targets, independent of the selector | Literal current source plus hashes, line numbers and Git snapshot. |
| `auto` | Deterministic selection from public failure output and path inventory only | Same packet format; no packet when evidence produces no supported source target. |
| `wrong` | Frozen, valid, plausible source targets disjoint from required edits | Same packet format. No arm label or warning that the targets are wrong. |

Unlike the old screening, control does not receive oracle scope. All arms get the
same request, public failure evidence policy, test command, permissions, governor,
model, effort, tool inventory and generation ceilings. Public failure evidence
must be captured in full, not the existing runner's 1500-character tail. All arms
may inspect further files. No model summaries, retrieval model or paid target search.

The initial failure output is captured from the identical fixture and shared across
arms, normalizing only the absolute workspace prefix to a relative path. In later
dependent steps, capture failure output from that episode's current edited source;
traces and line numbers can differ because prior agent edits differ. Do not replace
them with oracle traces to manufacture identical evidence. Record this endogenous
evidence difference as part of the sequential treatment. Source packets alone differ
at initial assignment; subsequent state differences are outcomes of that assignment.

Freeze installed CLI versions before pilot. Planned models are the historical
`claude-opus-5-5[1m]` and `gpt-6.1-sol`, medium effort, governor 183000. If unavailable,
pause for an amendment rather than silently substitute. Codex must use explicit
`model_auto_compact_token_limit_scope="total"` with the threshold as per-run options,
respecting `cbf3e56`; do not write installed settings. Exclude unrelated hooks and
plugins identically across arms. Meter observations are host-side, not extra model
tools or prompt content. Record resolved versions, actual options and compactions.

Ceilings are identical across arms: Claude 12 model turns and $3 provider-reported
USD per generation, both providers 300 seconds per generation, one attempt per
scheduled step. These are failure guards, not expected consumption. Budget limits
below are stricter cumulative stop allowances. A ceiling hit remains a failed
attempt with its consumed cost. No hidden-test-driven repair call is permitted.

Packets use the existing literal builder, with a 24000-byte source-and-metadata
budget; common request/failure evidence is measured separately and is identical
across arms. Target count is at most four. Fixture eligibility must establish that
oracle and wrong packets fit without truncation. Wrong excerpts must be within
20% of oracle source bytes using ranges around the frozen wrong symbols. Record
actual packet bytes; auto receives only evidence-selected content and may be shorter.
If auto candidates exceed the budget, allocate deterministic bounded ranges before
building, recording excluded candidates. Do not catch a builder error and silently
replace it with control. An evidence-empty auto selection is a declared outcome,
not an error fallback.

### Deterministic auto selection contract

Implement a small pure, strictly typed function in `bench_preparation.py`. Inputs:
full failing-test text, explicit source-root prefixes, and a sorted relative source
file inventory. Outputs: immutable targets and evidence/rejection records. No
mutation descriptions, correct targets, reference patch, hidden tests, source
content, model call or successful-test output may influence selection.

1. Parse Python `File "...", line N` and pytest `path.py:N:` frames. Normalize only
   paths inside the attempt workspace; reject traversal and external frames.
2. Prefer production traceback frames, ranked 100. Public test frames are evidence
   for module mapping, not source excerpts in the auto packet.
3. For failing node IDs such as `tests/test_itsdangerous/test_signer.py::...`, strip
   `test_` from the filename and search inventory for a unique `signer.py` under the
   declared production roots. Rank unique module matches 50. An ambiguous mapping
   yields a rejection record, never a guessed module. This also maps `test_more.py`
   to `more_itertools/more.py` and `test_iterutils.py` to `boltons/iterutils.py`.
4. Deduplicate by source path/line. Sort by descending rank, then path and line.
   Production frames become literal ±30-line ranges. A module-only match becomes
   lines 1–60 of that module, explicitly recorded as weak localization. Allocate
   at most four ranges, narrowing deterministically to fit the packet budget after
   selection. Reading selected source for excerpt construction is allowed; reading
   it to invent additional targets is not. Preserve the ranked candidate list.
5. No supported candidates means an empty selection and zero source bytes. The
   common task/evidence still reaches the agent. Report how often this happens.

Range bounds are clamped during source construction, not inferred from the gold
mutation. Test names alone can miss a function deep in a file; that is a measured
limitation of this deliberately modest heuristic. Selector regression tests use
fixed failure strings and real temporary inventories, with no provider calls.

## Task inventory and hidden acceptance

Repository commits verified by local source/Git inspection:

| Repository | Version | Resolved commit |
|---|---|---|
| more-itertools | v11.1.0 | `64be96ceb2a6e836f76f069f4a96d2394d59fd0c` |
| boltons | 26.2.0 | `4332b35a278d694f30c99881faa61cde695c7a96` |
| Itsdangerous | 2.2.0 | `096c8d42545d3b68ea21a4f890fb2b2d8979c0bd` |

Create six `bench/tasks/preparation-ab-*.json` fixtures after approval. Python 3.12,
exact dependency locks and no editable install pointing outside the attempt.
Itsdangerous has a `src/` layout and requires pytest/freezegun for its public suite;
install the attempt's package with recorded build dependencies or configure its
`src` import path consistently for agent and host. The current branch-clone helper
cannot directly clone an arbitrary commit SHA: fetch the version and verify the
resolved commit before copying the isolated workspace.

| ID | Task, generation steps | Oracle targets; plausible wrong targets | Hidden cases and reference basis |
|---|---|---|---|
| T1 `mi-count` | One step: historical `exactly_n` off-by-one mutation, generic prompt without named oracle scope. | `more_itertools/more.py::exactly_n`; wrong same-module `ilen`/bounded excerpt selected before runs. | Empty input, zero count, truthy/falsy mixtures, finite one-shot generators and consumption boundary. Byte agreement with pinned production reference. |
| T2 `boltons-ranges` | One step: `chunk_ranges`, `>= input_stop` changed to `> input_stop` at the aligned initial-chunk boundary. | `boltons/iterutils.py::chunk_ranges`; wrong `chunked_iter` excerpt. | Exact stop, nonzero offset, overlap, aligned and unaligned endpoints, zero input; reference output grid plus byte agreement. |
| T3 `id-salt-rotation` | One step, multifile: `Serializer.make_signer` ignores explicit salt; `Signer.verify_signature` checks only newest key. **Pilot task.** | `serializer.py::Serializer.make_signer`, `signer.py::Signer.verify_signature`; wrong `Serializer.dump_payload`, `Signer.validate` excerpts. | Explicit/default salt separation, bytes/text salts, old/new key rotation, modified-token rejection and combined cases. Both required production files restored; byte reference agreement. |
| T4 `id-mixed` | One step, mixed target forms: compressed payload marker disabled and base64 padding modulus changed from 4 to 3. | `url_safe.py:55:69` plus `encoding.py::base64_decode`; wrong range in URL-safe class declarations plus `encoding.py::int_to_bytes`. | Compressed/uncompressed payloads, encoded lengths modulo four, binary data and malformed input error class. Byte reference agreement. |
| T5 `id-stale` | Two dependent steps: repair compression marker and extract `_compress_payload(json: bytes) -> tuple[bytes, bool]` above the mixin, preserving wire bytes; then host changes the helper's compression threshold from `len(json)-1` to `len(json)+1`. | Step 1 range `url_safe.py:55:69`; step 2 new helper symbol and `URLSafeSerializerMixin.dump_payload`. Wrong current `load_payload`/class declaration excerpts. | Current-source hash changes, original line range displaced, compression threshold and legacy token wire compatibility after both edits. Refactor shape plus gold behavioral equivalence, not byte equality. |
| T6 `id-memory` | Four steps in one carried session: fix key rotation; repair timestamp equality boundary (`>` to `>=`); repair integer encoding (`lstrip` to `rstrip`); repair default key selection (`[-1]` to `[0]`). | `Signer.verify_signature`, `TimestampSigner.unsign`, `encoding.py::int_to_bytes`, `Signer.derive_key`, respectively. Wrong `Signer.sign`, `TimestampSigner.sign`, `bytes_to_int`, `Signer.validate` excerpts. | Step 1 states legacy keys remain accepted, newest key signs, `~` separators and existing wire bytes remain compatible; later prompts omit this constraint. Check it at each step and final closure with old fixtures, expiry edges and zero/trailing-zero integers. Byte/behavior reference checks per step. |

Itsdangerous paths in this table are relative to `src/itsdangerous/`. Excerpts around
wrong symbols are bounded to meet the size criterion without including mutation
lines. T1 is an anchor to the historical family; T2–T6 add five different episodes.
Tasks share repository provenance and some mechanisms, limiting independence and
external validity. Six task clusters do not represent six independent repositories.

Eligibility is a **local, no-model gate before the pilot**: pinned original passes
all public and hidden tests; each intended defect fails its declared public
regression; gold repair passes; wrong targets are valid/disjoint; packet sizes fit;
test integrity detection works. Add minimal public regression fixtures when upstream
tests do not expose a mutation (especially key choice and compression boundaries).
Agent-visible tests are frozen per step. Record host changes between steps separately
from unauthorized agent test edits. Exact match sites and failure counts are not
claimed validated by this design inspection. If an episode's required helper was
not created or an earlier step fails, mark the episode failed and leave remaining
steps uninvoked, with explicit reason and zero additional spend. Do not invent a
replacement mutation or secretly repair an agent's work.

Hidden tests and reference patches are absent from the agent workspace and prompt.
Materialize hidden validation only after the agent process exits, in a separate
host validation copy; remove it before a later step. Do not feed hidden failures
back to the session. Retain hidden fixture hashes, case counts and host reports.
The agent has no exposed hidden-test command or reference Git history. These are
evaluation visibility controls, not a claim of a security boundary against an
adversarial agent with unrestricted host access; use workspace confinement where
supported and audit external reads. A detected hidden-test exposure invalidates
the affected episode as a protocol violation and remains in the report.

Acceptance requires all public tests, unchanged agent-visible test hashes, all
hidden tests, and task-specific reference agreement. Also record these four flags
separately. For T5, reference agreement means required helper shape, unchanged API
and gold outputs/wire bytes on frozen fixtures; alternative correct refactor text
is allowed. All step-level and final checks are retained. No agent self-reported
success substitutes for host acceptance.

## Schedule, n and cache/order controls

Pilot: **T3 only, four arms × two agents × one episode = 8 episodes / 8 generation
invocations**. Claude order C–O–A–W, Codex W–A–O–C. Run serially, no provider warmup.
This is a measurement smoke pilot, n=1 task; no effect CI or repeatability claim.

Full study: **six tasks × four arms × two repeats × two agents = 96 episodes**.
There are ten planned step invocations across the six tasks (1+1+1+1+2+4), hence
**80 maximum generation invocations per agent, 160 total**. Native requests within
an invocation are measured separately; this count is not a token/request prediction.
Pilot outcomes are not pooled into the full study.

For each task/agent, freeze an eight-episode palindrome. Base order is
`C O A W | W A O C`. Rotate the first four positions by task index modulo four;
reverse them for the second block. For the other agent, reverse the first four
positions before constructing its reflected second block (reversing a complete
palindrome would leave it unchanged). For each treatment/control comparison, the
four relevant positions form
ABBA or BAAB. Enumerate the exact schedule in a committed manifest before generation,
using seed `20261005` for task ordering; alternate which agent runs first by task.
The first repeat is paired with its reflected second-repeat position. All jobs run
serially (concurrency 1), not as four live agents.

Each episode has a fresh source copy, Git history containing only the frozen fixture,
venv and CLI session. Steps within T5/T6 share that episode's workspace/session;
other episodes share neither edits nor session history. Execution cwd must contain
the exact lowercase marker `/tmp/cimrihook-bench/` so existing doctor/limits exclusion
works; do not use this worktree's mixed-case path as an agent cwd. Add regression
checks that all four arms stay excluded. Freeze quota weights before the pilot,
not using benchmark sessions to refit them.

Fresh workspaces do not make provider caches cold. Record cache reads/writes,
first-request cache status, input/output/reasoning tokens, packet bytes, invocation
timestamps, episode position, session IDs and reset boundaries. No cache flushing
or paid warming. Report first/reversed block results as an order diagnostic. Pause
across a quota reset rather than splicing a delta across windows; a split schedule
remains documented and does not imply perfect cache control.

T5 validates every new packet against current source/Git hashes before the next
invocation. Keep earlier packets in the archival session history; no controller
rewrites them after edits. Record source-hash changes, displaced original range,
packet rebuilds, failed native edits and host acceptance. This measures the
implemented per-step rebuild policy and history exposure; it does not prove stale
context was erased during a generation.

## Outcomes and analysis fixed before runs

For each attempted episode retain all step costs, provider counters, wall time,
request counts, quota snapshots, status/error, public/hidden checks, integrity,
reference agreement, acceptance and uninvoked steps. Retries would change the
sample and need a new approval/amendment. Never replace failed episodes with easier
ones, omit max-turn/timeouts, or reinterpret missing telemetry as zero cost.

For agent p and task t, average the two repeats for each arm over whole-episode
cost, including failures and consumed earlier steps. Primary paired difference:
`d[t] = mean(cost_auto[t]) - mean(cost_control[t])`. Report the six differences,
their equal-task mean and percent differences against control. For scale comparability,
also report the equal-task mean of `d[t] / mean(cost_control[t])`; undefined zero
denominators are explicit, not dropped. Oracle and wrong use the same construction.

Bootstrap **tasks**, drawing six task IDs with replacement, 10000 draws, seed
`20261005`; keep both repeats, every arm and all steps within the drawn task.
Report percentile 95% CIs. Do not resample requests or steps as independent work.
Report accepted-episode rate and paired acceptance differences with the same task
resampling, and accepted episodes per total cost as a supplementary efficiency
measure including failed spend. No confirmatory p-value or optional stopping for
significance. Report repository provenance and the small-cluster limitation beside
the CI. Cross-agent differences and repo-level slices are descriptive.

Known-cost failures contribute their actual measured cost and zero acceptance.
If a scheduled episode lacks measurable cost, retain its native artifacts and mark
cost unknown; the primary estimate/CI is unavailable until recovered from native
logs without another generation. If the approved study stops early, report all
attempts and descriptive complete pairs, explicitly mark the planned study incomplete
and do not present a complete-study primary CI. Uninvoked steps following a known
quality failure have no additional spend; their episode remains a failure with the
cost already consumed. Do not reuse generic reports that filter `result.error` rows.

### Cost definitions and calibration snapshot

Read-only command executed in stage 1:

```bash
uv run cimrihook limits --agent codex --days 30 --json
```

Its first snapshot reported five-hour 1650 spans / 2215 points, 98799.7865 effective
input tokens or 5081.5447 output tokens per point; weekly 354 spans / 482 points,
separate weights null and 442340.2126 pooled base units per point. A later read of
the same fitting functions at **2026-10-04T23:18:23.458523Z** exposes coefficient CIs
and supplies the frozen budget coefficients below. The live-log snapshots differ
in the five-hour fit (1655 spans / 2220 points); weekly is unchanged. Do not combine
coefficients from different snapshots or infer benchmark spend from that difference.
The coefficients' CIs are the existing fitting model's 95% intervals, not new study
bootstrap intervals or out-of-sample forecast intervals.

Let `U` be uncached input, `C` cached input, `O` total output. Effective input is
`I = U + W + 0.1*C`, where W is cache-write input (zero in the historical Codex pilot). Preserve reasoning as a separate diagnostic; do not add it twice
if included in provider output. Preserve cache-write counts where the provider has
them. Codex primary predictor:

`P5h = 0.000010122022951731196*I + 0.00019682102568602472*O`.

| Calibration quantity | Estimate | Existing 95% coefficient interval |
|---|---:|---:|
| Five-hour input points / effective token | 0.00001012202295 | [0.00000877494616, 0.00001146909974] |
| Five-hour output points / token | 0.00019682102569 | [0.00014846189332, 0.00024518015805] |
| Weekly pooled points / `(I + 6*O)` unit | 0.00000226070335 | [0.00000209938162, 0.00000242202508] |

Thus about 98794.5 effective input or 5080.8 output tokens per five-hour point;
output/input weight ratio is **19.44**, approximately 20. Raw input tokens do not
all count at the uncached rate. Weekly output coefficient interval is
`[-0.00001225430356, 0.00003388963703]`, crossing zero: **separate weekly weights
are unidentifiable with these observations**. Do not transfer the five-hour 20×
ratio to weekly. Report weekly `P7d_proxy = 0.00000226070335*(I+6*O)` and its pooled
coefficient interval, with the fixed 6× aggregation assumption explicit. It is not
a guarantee about the true weekly charge of an output-heavy run.

Also report Codex fixed API-equivalent units `I + 6*O`, with sensitivity
`I + 8*O` and `I + 20*O`. These use the repository's frozen legacy price ratios,
not current model-specific USD or an actual subscription bill. For the historical
control these are 33397.8 / 34917.8 / 44037.8 units. Study task-bootstrap CIs hold
calibration fixed; report calibration uncertainty separately, never conflate them.

Claude primary cost is the provider-reported USD counter (subscription-authenticated
API-equivalent billing). Keep input/cache-write/cache-read/output counts and resolved
price metadata. Native before/after utilization deltas are separate account-wide
observations. Label attributable only with pre-request and post-run readings within
one reset window, adequate resolution and no concurrent provider work. Otherwise
write `not attributable`, never convert rounded deltas into cost savings.

## Consumption estimate and stopping rules

Historical source: `bench/results/preparation-screen-20261005/`. The higher-cost
historical control invocation supplies every arm's planning rate: Claude $0.090335;
Codex U=17625, C=112128, O=760, I=28837.8. No assumed preparation saving. New tasks,
wrong targets and long carried sessions may exceed this rate. The **2× reserve is
an engineering allowance, not a statistical upper bound**.

| Stage | Invocations per agent | Claude expected / 2× reserve | Codex five-hour points expected / 2× reserve | Codex weekly pooled proxy points expected / 2× reserve |
|---|---:|---:|---:|---:|
| Approved measurement pilot | 4 | $0.3613 / $0.7227 | 1.7659 / 3.5318 | 0.3020 / 0.6040 |
| Proposed full study, excluding pilot | 80 | $7.2268 / $14.4536 | 35.3185 / 70.6369 | 6.0402 / 12.0804 |
| Both stages, requiring separate approvals | 84 | $7.5881 / $15.1763 | 37.0844 / 74.1688 | 6.3422 / 12.6844 |

The pilot's weekly proxy interval from pooled coefficients alone is
**[0.2805, 0.3236]** points; full study **[5.6092, 6.4712]**. Doubling the latter for
the reserve gives **[11.2184, 12.9424]**. These do not include variability in task
consumption. Five-hour pilot coefficient endpoint envelope is [1.4635, 2.0683],
full [29.2705, 41.3664]; it is an endpoint sensitivity envelope, not a joint 95%
prediction interval because coefficient covariance is not included.

As a consumption stress scenario, 4× the historical control rate would cost the
full study Claude **$28.9072**, Codex **141.2739 five-hour points** across windows,
and **24.1608 weekly pooled proxy points** (coefficient-only interval
**[22.4367, 25.8849]**). With the observed 58% weekly use and pilot reserve, this
would approach 82.8% weekly at the pooled central estimate, leaving very little
headroom for other work. This is not an approved allowance or a validated upper
bound. The pilot's single-step task cannot resolve carried-session growth in T5/T6;
the second approval must explicitly consider that remaining uncertainty.

Read-only native quota probes at 2026-10-04T23:14Z showed Codex primary **25%**,
weekly **58%** (weekly reset 2026-10-09T21:34:35Z); Claude five-hour **0%**, weekly
**57%**, model weekly **5%**. These are expiring snapshots, not reserved capacity.
Codex then had 27 weekly points before the 85% threshold. Full-study reserve plus
pilot would forecast roughly 70.7% weekly under the pooled proxy if nothing else
spent quota; that is not an execution authorization. Five-hour full-study reserve
cannot safely be added to 25% within one window: plan smaller blocks across resets.
Claude quota points are **not estimable** from the historical USD alone.

Prespecified safety and budget policy:

- Read native quota immediately before approval-stage execution, before each
  generation, and every 30 seconds while a child is running. If **any relevant
  weekly window exceeds 85%**, terminate the active process group and stop queued
  work. At exactly 85%, launch no new generation. Apply this to Codex weekly and
  Claude aggregate/model-specific weekly windows. Record partial consumption and
  quality status; no automatic resumption or replacement.
- Pause on unavailable quota, stale observations (>60 seconds at launch), telemetry
  failure, hidden exposure, changed model/config, or fixture/packet protocol error.
  No zero-cost fallback or unapproved recovery generation.
- Use 95% five-hour utilization as a launch/active stop guard; before each launch
  also require at least five remaining primary-window points. Start paired blocks
  only with capacity for their forecast plus reserve. Reset-spanning interruptions
  are recorded and shown before resumption approval; do not run unattended waits.
- Pilot cumulative launch allowances are Claude **$0.7227**, Codex predicted
  five-hour **3.5318 points** and weekly pooled proxy **0.6040 points**, with an
  absolute Claude per-generation limit of **$0.50** for this one-step pilot. Full
  allowances are provisional 2× reserve values, to be recalculated after the pilot.
  Before launching, remaining allowance must cover a reserved next invocation at
  the planning rate. After each generation, stop if any cumulative allowance is
  exhausted. No other arms are silently omitted from the report to stay on budget.
- The pilot $0.50 ceiling replaces the otherwise common $3 ceiling identically
  across its arms. Native per-generation ceilings and host stop checks may overshoot
  by an in-flight provider request. Actual native weekly >85% takes precedence over
  every predictor; cumulative point estimates are launch allowances, not enforceable
  exact provider quota caps. If the eight episodes cannot fit, stop incomplete and
  request approval with actual costs. Never start the full study at this boundary.
- Full study stops at its fixed n, quality/protocol/budget guard, or user cancellation;
  never when a desired result appears. Partial data, unsuccessful attempts and stop
  reasons stay in the result inventory. No extending n to narrow a CI without a
  separately approved preregistration.

**Stage-1 benchmark-generated spend: zero model invocations, zero benchmark window
points.** Planning conversation and other concurrent account activity are outside
this attribution; no claim is made that the account's utilization did not change.

## Required reporting

Archive the manifest, task/dependency hashes, packet/evidence artifacts and native
provider outputs under `bench/results/preparation-ab-<stage>-<date>/`, with isolated
execution under `/tmp/cimrihook-bench/`. Extend `bench-report` to use the dedicated
preparation analysis without changing historical report semantics. Use existing
`bench-calibrate` only after approval, as a separate compaction diagnostic; a
no-compaction result is not evidence for or against source-preparation savings.

Update `docs/evaluation.md` after the pilot and, if authorized, after the full study:
attempted/completed/accepted counts, n, task-bootstrap CI or why unavailable,
all four arms, per-task quality, cost definitions, 6/8/20 sensitivity, observed
vs predicted window points, stops and limitations. Keep **established**, historical
**screening pilot**, new **measurement pilot**, and preregistered exploratory full
study distinct. Answer each uncertainty, explicitly leaving it unresolved when
the sample or measurement does not support an answer.

## Execution freeze after first approval

The user explicitly approved the measurement pilot with “Onayliyorum.” The pilot
remains one T3 episode per arm per agent, eight scheduled episodes and no efficacy
CI. The full manifest remains `execution_approved=false`. No model invocation had
started when this execution amendment was written.

Local eligibility established all six tasks / ten steps: original and reference
public suites pass, each individual mutation produces finite public failures,
and reference fixes pass public, private, integrity and reference checks. The
machine-readable evidence is `bench/preparation/eligibility-20261005.json`; raw
host evidence is retained at `/tmp/cimrihook-bench/preparation-eligibility-final-20261005/`.
Expected initial failure counts are T1=2, T2=1, T3=15, T4=235, T5=25, T6=4.

Before generation, local representation corrections resolved the src-layout
editable install for Itsdangerous, selected the real implementation behind
`@overload` declarations, and cleared stale bytecode after same-size fast edits.
Wrong ranges are frozen offsets around the already specified wrong symbols,
disjoint from oracle excerpts and within 20% of oracle source bytes. T4's
`URLSafeSerializer` range ends at the actual file end (line 83). T6 step 2 also
uses `Signer.unsign` as wrong context to satisfy the size match. T5's second
mutation locates the extracted helper with AST positions, so formatting cannot
prevent the mutation. These changes preserve tasks, arms, n, metrics and allowance.

Auto budget fitting halves the lowest-ranked remaining range around its midpoint
until the complete source-and-metadata body fits 24000 bytes; a one-line range
that still exceeds the budget is explicitly excluded. The fitted ranges and
narrowing/exclusion reasons are archived. This never invents another target.
Prompt text is sent through stdin to avoid OS argument-size limits. The complete
common public failure output remains identical across first-step arms.

Before each task block, reserve 2x the historical Codex per-call rate for every
scheduled step in that block. Five-hour capacity must remain below 95%; the
weekly pooled coefficient upper endpoint must remain below 85%. Claude point
forecast is unavailable; enforce measured windows and the approved USD allowance.
During generation both providers' relevant native windows are checked, including
Claude model-specific weekly windows. Reset timestamp changes beyond 60 seconds
of rounding interrupt the block. Failed quality or a known-cost timeout/turn
ceiling ends that episode; its spend remains in the analysis. Protocol, telemetry,
provider, quota or cumulative allowance errors stop the study without replacement.

Complete failure evidence sizes from local eligibility are T1=2038, T2=2458,
T3=11144, T4=592820 and T5 first step=102970 bytes; T6's four steps total 11410
bytes. T4 and T5 substantially exceed the pilot input. The historical flat rate
is not a validated upper bound for the full study, even with 2x reserve. The
second approval must consider these inputs and carried-session growth; this
amendment does not silently truncate evidence or authorize the full study.

Host Python is pinned to the fixture's Python 3.12 environment; actual patch
version and installed package versions are archived per episode. Repo SHAs,
task hashes, implementation commit/source hashes, CLI versions, schedule and
provider counters are retained. `bench-calibrate` reports measured compaction
counters separately and makes no simulated preparation-savings claim.

The first execution preflight stopped while writing a forecast artifact because
the filename suffix lacked its leading dot. No provider generation command was
constructed or launched; no episode exists. That zero-generation preflight is
archived with the `-preflight` suffix. The corrected block preflight is exercised
by a local file/record integration test before the first pilot generation.
This is an infrastructure correction, not a replacement of a model attempt.
