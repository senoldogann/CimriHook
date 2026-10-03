# CimriHook

> *cimri* (Turkish): miser. CimriHook makes AI coding agents never pay twice for the same information.

AI coding agents keep re-sending information their context window already holds. They re-read files
that have not changed, re-read a whole file after a two-line edit, re-run a command and paste the
same 300-line output again, or read a 3,000-line file to find one function. Every token that enters
the context is paid again on every following request (as a prompt-cache read) until compaction.

Measured on real sessions, 96–98% of an agent's input tokens are such re-reads of context it
already has, not new information. Shrinking each new tool output (what RTK does) helps, but the
largest lever is **how long content stays in the context**. CimriHook works on all three layers:

| Layer | What it does | How |
|---|---|---|
| Measure | replays your own transcripts: where the money goes and what each policy would cost | `cimrihook doctor`, `cimrihook audit`, `cimrihook simulate` |
| Govern | picks the auto-compaction window from your data and applies it with Claude Code's own setting | `cimrihook settings --compact-window N` |
| Encode | re-encodes new tool results against what the agent already holds (lossless) | `PostToolUse` hooks |

It complements RTK: RTK shrinks what enters the context, CimriHook decides how long it stays and
never sends the same information twice.

## Govern the context window

```bash
cimrihook simulate --days 30                   # cost of each compaction policy on your sessions
cimrihook settings --compact-window 183000     # adds CLAUDE_CODE_AUTO_COMPACT_WINDOW
cimrihook bench-calibrate --name <set>         # simulated vs measured savings of an A/B set
```

`simulate` rebuilds the real request sequence from the usage logs, prices each session with its
own model's cache multipliers and checks the replay against the exact cost recorded there. It then
replays the sequence under each policy. The cost of a compaction comes from the real compactions in
the logs: the summary request reads the context once and writes the summary at the output price,
and the next request carries the new context (system prompt, tools, summary and re-attached
files), of which only the part that is no longer cached is written again. Simulated savings assume
the agent behaves the same otherwise. `bench-calibrate` replays the baseline runs of an A/B set at
the treatment's real trigger point and prints how far the prediction is from the measured ratio.
Check task quality with an A/B run before adopting a small window.

Claude Code compacts once the context reaches the window minus 33,000 tokens (a 20,000-token
output reserve and a 13,000-token buffer) and raises windows below 100,000 to 100,000, so
compaction cannot start before about 67,000 tokens. `simulate` prints the setting for its best
policy.

## Evaluate with your real subscriptions

```bash
cimrihook bench-run --name claude-ablation --agents claude --protocols sequential \
  --variants baseline,governor,codec,combined --window 100000 --reps 5
cimrihook bench-run --name codex-governor --agents codex --protocols sequential \
  --variants baseline,governor --window 60000 --reps 5
cimrihook bench-report --name claude-ablation
```

Every task in `bench/tasks/*.json` is a real repository at a pinned tag with one-line bugs
injected. A run succeeds when the repository's own test suite passes after every step and no test
file was touched. The harness runs each task with your logged-in Claude Code (`claude -p`) and
Codex CLI (`codex exec`) in one arm per mechanism:

| Variant | Claude Code | Codex CLI |
|---|---|---|
| `baseline` | default behaviour | default behaviour |
| `governor` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (at least 100000; compacts at window − 33k) | `model_auto_compact_token_limit` |
| `codec` | codec hooks only | not available (hooks cannot rewrite tool output) |
| `combined` | window and codec hooks | not available |

- **Isolation:** every run gets its own workspace and virtual environment. Agents get only an
  allowlisted environment (no inherited `CLAUDE_CODE_*`/`ANTHROPIC_*` variables). Claude Code
  runs with project settings only and `--strict-mcp-config`; Codex runs with
  `--ignore-user-config`.
- **Primary cost:** what the provider bills, including compaction and auxiliary requests. For
  Claude Code this is the cumulative `total_cost_usd` of the last step (USD). For Codex it is the
  sum of the rollout's per-response `token_usage_record` entries, priced with the sheet that is
  stored in each result (base input units). The report also prints the Codex ratio under a range
  of price sheets.
- **Transcript cost:** `cost_base` sums only the requests in the session log (the Claude
  transcript including subagents, or the Codex `token_count` events). It leaves out the
  compaction request, so the gap between the two costs is the compaction overhead.
- **Statistics:** each scenario compares geometric mean costs with a 95% Welch t interval on log
  cost; no interval is given when an arm has fewer than two runs. The per-agent summary weights
  scenarios equally and uses only scenarios measured equally often in both arms. Step success is
  compared with a Newcombe interval. Non-inferiority at −5 points is only decided with at least
  five runs per arm. Cumulative costs at steps 5/10/20/40 come from the same runs.
- **Long sessions:** the `sequential` protocol injects the bugs one at a time into the same session,
  so the context accumulates the way it does in real work.
- **Resumable:** results are written per run, so an interrupted batch picks up where it stopped.
  Runs that hit a usage limit (a failed turn, or a step without model requests) are recorded as
  unmeasured and re-run.
- **Re-measuring:** `cimrihook bench-remeasure --name <set>` recomputes a result set from the
  agents' logs with the current schema without running the agents again.

## Encode tool results

The context codec works on the tool layer, so it behaves the same with every model and every effort
level. The tool always really runs. Only the *encoding* of its result changes, relative to what the
agent already has in its context:

| Encoding | When | What the model receives | Information |
|---|---|---|---|
| REF | the result is identical to text the agent already received in this context (an earlier Read covering these lines, or the previous output of the same command) | a one-line reference | lossless |
| DELTA | the result changed compared with the last full version (keyframe) the agent received | a unified diff against that keyframe | lossless |
| OUTLINE | the agent asks for a whole large file (≥ 6k tokens) it has never seen | declarations with line numbers and how to read ranges | recoverable |

## Safety rules

- **Nothing is hidden twice.** If the agent repeats a request whose answer was re-encoded and nothing
  changed in between, it gets the raw result. A request gets an outline at most once.
- **Context-aware invalidation.** Knowledge is tracked per context window (main conversation and
  each subagent) and per generation. `SessionStart`, `PreCompact` and compaction boundaries written
  to the transcript start a new generation.
- **Depth-1 deltas.** A delta always references the last raw keyframe, so the model never chains diffs.
- **Fail-open.** CimriHook errors exit with status 1. Claude Code treats that as a non-blocking hook
  error and uses the original tool output.
- **Complements Claude Code.** Claude Code itself answers exact re-reads of unchanged files (same
  offset/limit, same mtime) with `file_unchanged`. CimriHook covers what that misses: changed files,
  sub-ranges, mtime-only changes and command output. If the agent only ever saw an outline,
  CimriHook replaces the native `file_unchanged` answer with the real content.

## Install

```bash
uv tool install --editable /path/to/CimriHook
cimrihook settings          # prints the hooks block for Claude Code
```

Merge the printed `hooks` block into `~/.claude/settings.json` (all projects) or
`.claude/settings.json` (one project). To try it for a single run:
`claude --settings "$(cimrihook settings)"`.

## Measure

```bash
cimrihook doctor --days 7   # where your spend goes and what would change it
cimrihook audit --days 30   # replay your past transcripts: what would CimriHook have saved?
cimrihook report            # savings recorded by the live hook
```

`doctor` prices every real request in your Claude Code transcripts at API list prices and splits
the spend by the context size of the request, by token type, by cache rewrites over 100k tokens
and their likely cause (idle past the cache lifetime, compaction, model switch), by main session
and subagents, and by the static prefix every request carries. It ends with what would change the
largest items; the compaction-window estimate comes from `simulate` and is labelled as a
simulation until an A/B run confirms it.

`audit` runs the same codec over the tool results stored in `~/.claude/projects`. Subagent
transcripts do not store the structured tool result, so their Read and Bash results are parsed
from the text the model received. It reports direct token savings and a context-residency
weighted share of your input cost. The weighting reflects
that each saved token would have been re-read from cache on every later request until compaction,
priced with the 5-minute/1-hour cache-write mix found in your own usage data.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CIMRIHOOK_HOME` | `~/.cimrihook` | ledger directory (SQLite) |
| `CIMRIHOOK_DISABLE` | empty | comma list of `ref,delta,outline`, for ablation studies |
| `CIMRIHOOK_OUTLINE_MIN_TOKENS` | `6000` | outline threshold |
| `CIMRIHOOK_DELTA_MAX_RATIO` | `0.5` | send a delta only if it is at most this share of the raw result |
| `CIMRIHOOK_MIN_SAVING_TOKENS` | `150` | do not re-encode for smaller savings |

## Limitations

- When a Bash command exits non-zero, hooks only receive a plain-text error
  (`PostToolUseFailure`), which they cannot rewrite. Failing test runs pass through unchanged.
- Claude Code can clear old tool results in memory (microcompaction) without writing a marker. If a
  REF or DELTA points at text the model can no longer see, the model repeats the call and gets the
  raw result.
- The ledger keeps the text the agent received, zlib-compressed, in `~/.cimrihook/ledger.sqlite3`.
  Claude Code's own transcripts store the same text.
- Token numbers in reports are estimates (~4 characters per token). Evaluations should use the
  usage numbers reported by the API.

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```
