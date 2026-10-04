# Development priorities

Review date: 4 October 2026. Objective: complete more useful work within the same subscription
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

The next small action is a manual prefix comparison using the existing
breakdown. Codex diagnosis follows only after the savings path is evaluated. A dynamic
controller, task classifier, centralized telemetry and new CI commands are deferred.
