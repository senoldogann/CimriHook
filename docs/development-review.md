# Development priorities

Review date: 5 October 2026. Objective: complete more useful work within the same subscription
allowance, retaining task quality. API list-price cost, input size and account window use are
different measurements. The screenshot's `API estimate` is not a subscription bill.

## Review of the proposed six-part plan

| Proposal | Assessment | Smallest useful action |
|---|---|---|
| Prefix trim and cache-bust detection | Useful diagnosis, partly implemented. `doctor` already prices the estimated prefix above bare Claude Code; the mod records tool/memory categories. The claimed 8–15% saving is unmeasured. Unused tools are not necessarily unnecessary. | Use the existing breakdown to identify one optional large component, then measure a reversible manual removal on matched tasks. No separate prefix command or automated disabling yet. |
| Thinking profiler and effort advice | Useful only where the provider exposes a separate reasoning counter. Visible thinking is not a reliable billed-token measure. The claimed 15–25% saving is unsupported. | Display explicit provider counters when available; otherwise report unknown. Test effort changes on a fixed workload before making recommendations. |
| Dynamic compaction windows | A hypothesis with substantial complexity. Context growth and changed-file count do not establish what information a later task needs. No policy can promise zero context loss. | Keep the measured fixed governor. Test verified task closure against it before adding a controller. |
| Codex doctor | A reasonable later extension. Codex simulation, benchmark parsing, threshold configuration and direct quota probes already exist; the full diagnosis is Claude-only. Refetch is overhead, not proof of a memory leak. | Reuse the existing Codex parsers and show available counters, compactions and refetch. Keep model-specific API estimates distinct from subscription observations. |
| CI health check and team reports | Does not currently establish more completed work per subscription. A dollar threshold on an API estimate is not an allowance threshold. | Keep local reporting. Add CI or team aggregation when there is a concrete team requirement. |
| Paper and public release | Reproducible measurements can support a research contribution; venue suitability and novelty are not established by a small benchmark. | Publish precise supported results when desired. Avoid universal quality guarantees and claims that RTK cannot solve a category of costs. |

The historical 16.6k bare-prefix baseline and 34.4k first-request estimate are environment and
version dependent. First-request input can include user content. The excess-prefix cost is an
allocation estimate, not a measured recoverable saving or an exact cache-loss attribution.

## Source corrections

- **Caching.** A change invalidates the matching prefix from the divergence/cache boundary;
  it does not necessarily erase every earlier cached segment. Five-minute and one-hour
  Anthropic writes cost 1.25 and 2 times base input respectively. A local prefix hash alone
  cannot attribute an exact dollar loss: TTL, model, effort, tools and cache boundaries matter.
  The provider now offers cache diagnostics for API requests. [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
- **Context editing.** The documented strategy names are `clear_tool_uses_20250919` and
  `clear_thinking_20251015`, under `context-management-2025-06-27`. These are API capabilities;
  their availability does not establish that a Claude Code hook can set them. Clearing earlier
  blocks can also disrupt caching. [Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing).
- **Reasoning.** Omitted or summarized thinking is still billed at full generated thinking
  usage. Counting visible text cannot recover an exact reasoning/answer split.
  [Anthropic thinking](https://platform.claude.com/docs/en/build-with-claude/thinking).
- **OpenAI.** Current API models can have cache-write prices, but Codex credit billing has no
  separate cache-write charge. Credit prices do not by themselves determine included
  subscription use. Cached-input ratios also vary by model: the current GPT-6.1 Sol credit
  ratio is 2.5/50, rather than a universal 10%.
  [Codex pricing](https://learn.chatgpt.com/docs/pricing),
  [OpenAI API pricing](https://developers.openai.com/api/docs/pricing).
- **Observation masking.** *The Complexity Trap* compares masking with raw histories and
  LLM summaries in SWE-agent/SWE-bench Verified, with initial OpenHands evidence. Its roughly
  halved cost is not an incremental saving proven on CimriHook's existing governor or on
  subscription windows. [arXiv 2508.21433](https://arxiv.org/abs/2508.21433).
- **RTK reference.** arXiv 2607.12161 is *Token Reduction Is Not Cost Reduction*, an empirical
  comparison including RTK, RTK-ML and Headroom. It reports that stronger compression can cost
  more by changing retrieval, testing and turns; its result supports measuring successful-task
  cost, not treating fewer delivered tokens as savings. [arXiv 2607.12161](https://arxiv.org/abs/2607.12161).

## Current decision

The matched comparison against the existing governor is complete, with fixed model/effort/window
and unchanged repository tests. Per-task closure increased cost. Five-task closure has a small
observed reduction in two pairs, whose interval includes no saving; subscription savings remain
unestablished. Closure stays explicitly enabled only in the isolated experiment. Measurements
and their limits are in [evaluation.md](evaluation.md#verified-closure-on-real-sequential-tasks-2026-10-04).

The prefix comparison is complete: one separate 20-task pair observed 6.54% lower API-equivalent
cost with Read/Edit/Bash/Glob/Grep, with identical final Python sources and all original tests
passing. It is a small profile result, not the proposed general 8–15% or a subscription saving.
Use the existing tool flag for known suitable tasks. The existing governor benchmark already
used this profile, so its older savings and this result are not additive. `doctor` now reports
category prevalence to avoid treating one session's large MCP category as universal.
Details and limitations are in [evaluation.md](evaluation.md#focused-tool-profile-on-real-sequential-tasks-2026-10-04).

## Review of the X suggestions and auto-handoff

| Suggestion | Decision for CimriHook |
|---|---|
| Strong model for planning, other models for implementation | A workload-specific A/B candidate. Switching models does not bypass the account's shared limits; model-specific allowances may differ. Delegation adds its own usage and coordination work. Keep an explicit model choice until unchanged tests and task acceptance show equivalent quality. |
| Low effort by default | Offer a profile for suitable tasks after measuring it. Do not change the tested medium-effort default globally. The focused-tool pair's explicit thinking counters were 333 and 568 tokens, about 2.8% and 4.2% of output; that pair did not compare effort levels. |
| Compact at 50–60% | Do not adopt a universal percentage. On a 1M window that means 500–600k tokens, later than the measured 183k/233k governor settings. Keep the existing absolute window and cache lifetime policy. |
| Remove duplicate skills | Review active instructions with the native prompt audit. Skills are loaded on demand; installed skill count is not a measure of full prompt cost. Removing an instruction because the model supposedly already knows it needs a quality check. |
| Disable unnecessary MCPs | Measure active categories and use a focused tool profile for suitable tasks. Tool search defers definitions by default; total installed schemas are not necessarily active context. The existing 20-task profile comparison is the relevant small observed gain. |
| Clean MEMORY.md / CLAUDE.md | Use native `/doctor prompt-audit` and review concrete edits. MEMORY.md has a bounded initial load, while CLAUDE.md imports are still loaded. No automatic semantic deletion or new cleanup model in the background. |

Primary references: [Claude Code costs](https://code.claude.com/docs/en/costs),
[memory and native prompt audit](https://code.claude.com/docs/en/memory),
[MCP tool search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search).

`alexknowshtml/claude-auto-handoff` was inspected at
[`ece296a3afe04852109891c93c071fd6f1167933`](https://github.com/alexknowshtml/claude-auto-handoff/tree/ece296a3afe04852109891c93c071fd6f1167933),
manifest version 0.8.5. It asks Haiku to write a structured brief, clears the conversation, then
seeds a new session with a pointer to the brief. Successful edits and commits help ground it;
handoff count and growth guards limit loops. The viewer, extra summarizer call and fresh-session
cache writes add work. Its context reduction is not a measured subscription saving. Installing
it alongside CimriHook would introduce a second context policy. No source code was copied;
the repository is MIT-licensed, so a future substantial copy must retain its notice.

The useful patterns are acting after a turn and respecting native disable flags. These exposed
two existing CimriHook issues: Claude Code rejects `session.compact()` inside `prompt.submit`,
and automatic mod calls did not honor `DISABLE_AUTO_COMPACT` / `DISABLE_COMPACT`. The unsupported
cold-prompt call is removed; the experimental threshold now runs after the main turn. The existing
cold-prompt guard remains the explicit recovery path. Automatic calls require an attached surface,
because `-p`/SDK does not support this API on 2.1.289. New `boundary` benchmark runs are rejected
before spending allowance; historical records remain readable.
[Native disable semantics](https://code.claude.com/docs/en/env-vars).

Verification: 17 cases pass through Claude's real hook test engine with controlled backend state,
covering turn timing, both disable flags, false flag values, manual compaction and no attached
surface. The pre-fix cold-prompt case produced the host's forbidden-hook error. A real isolated
Haiku CLI call then exposed the separate headless restriction. After the surface check, the
plugin loaded without errors, wrote an empty surface list and returned `READY.` without a
compaction error even with threshold 1. This proves the headless skip, not a production interactive
compaction or a new saving. Private evidence is under
`~/.cimrihook/experiments/compact-hook-20261005/`.

The already enabled local plugin was an older project version (`4f1d2d6`); its files were backed
up under that experiment directory and refreshed from the tested package. Plugin validation
passed, and the Claude settings file stayed byte-identical. New sessions load the refreshed mod;
an existing session must reload its plugins or restart to use it. Python tests: 77 passed;
Ruff passed; mypy passed for 44 files.

## Cache prewarming decision

Three mechanisms need separate measurements:

- Cold prewarming moves a cache write before the user's request, mainly reducing latency.
  Anthropic now documents `max_tokens: 0` prewarming with no generated output, but writes and
  reads are still charged. It does not by itself avoid the initial write.
- Refreshing a still-warm, unchanged prefix near expiry may avoid a later cold rewrite. It
  requires matching cache identity and provider support; an independent API request has not
  been shown to refresh a Claude subscription session. API rate limits and subscription
  allowance are different measurements.
- Warm compaction performs useful summarization before expiry and reduces later context, with
  a quality tradeoff. This is CimriHook's existing optional mod behavior, not a keep-alive ping.

[Anthropic prewarming and cache lifetime](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#pre-warming-the-cache).
The existing five-minute keep-alive replay in [evaluation.md](evaluation.md#ideas-that-did-not-pay-off)
was 3.9–70% more expensive than the one-hour baseline. It is a simulation of that policy, not a
rejection of every possible refresh policy or a subscription measurement.

The warm timer now uses the start of the latest main request with reported cache usage, rather
than turn completion. It does not infer a request start from a resumed response's age. Hook-engine
cases cover a 35-minute response (compact at request age 55 minutes), a response that outlasts
the one-hour TTL (skip), and no cache usage (skip). These are controlled timing checks; live
interactive warm-compaction savings remain unmeasured.
Current checks: 20 hook scenarios and 77 Python tests pass; Ruff and mypy pass. The installed
mod is refreshed with a backup; existing sessions need plugin reload or restart.

Feasibility (5 October 2026, from the 2.1.289 plugin API, no request sent): `$.model.fork`
re-sends the main thread's last request (model, system prompt, tools, messages) with one added
user message, so the provider serves that prefix from the session's own cache; its usage reports
how much was read. A short reply is unavoidable, since the call has no output cap. This is the
supported refresh path; `$.model.complete` has no history and cannot reach the session's cache.

Value, replayed on this machine's main-session transcripts (no request sent): one fork at 55 minutes
of idle, priced as a cache read of the previous context plus 50 output tokens, against the cold
rewrite the next request actually paid when it came within the extended hour.

| Period | Gaps over 55 min | Rewrite avoided | Net of refresh cost | Share of main spend |
|---|---|---|---|---|
| 30 days, mostly before the governor | 67 | 21 | $53–61 | 2.4–2.7% |
| 0.95 days since `cimrihook init` | 5 | 2 | $0.50–1.25 | 0.2–0.5% |

The lower bound also refreshes after every session's last request. With the governor, contexts are
small and an idle rewrite is cheap; `gain` shows idle re-caching fell from $20 to $3 in the same
period. Decision: do not build a refresh mod or spend allowance on a live test now. Revisit if
`doctor` shows idle rewrites growing again. The replay cannot see behaviour changes and assumes
each refresh reads the whole previous context.
Codex diagnosis remains the next product extension after this small cache feasibility check;
reuse existing parsing and quota probes. Automatic routing and centralized telemetry are deferred.
