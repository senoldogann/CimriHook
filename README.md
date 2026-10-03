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
| Govern | picks the auto-compaction window from your data and applies it with Claude Code's own setting | `cimrihook init --compact-window N` |
| Show | live context size, prompt-cache warmth, next-request cost and usage limits | `cimrihook statusline` |
| Encode | re-encodes new tool results against what the agent already holds (lossless) | `PostToolUse` hooks |

It complements RTK: RTK shrinks what enters the context, CimriHook decides how long it stays and
never sends the same information twice.

## Govern the context window

```bash
cimrihook simulate --days 30                   # cost of each compaction policy on your sessions
cimrihook init --compact-window 183000         # sets Claude Code's autoCompactWindow
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
So far its errors were all on the optimistic side (0 to 7 points), which is why the replay adds
7,000 re-read tokens after every compaction (the median the A/B runs imply); change it with
`--refetch-tokens`. Sessions are replayed whole, costs are summed in base input units across
models, and a forked or resumed session's copied history counts once. Check task quality with an
A/B run before adopting a small window.

Claude Code compacts once the context reaches the window minus 33,000 tokens (a 20,000-token
output reserve and a 13,000-token buffer). `init --compact-window` writes Claude Code's own
`autoCompactWindow` setting (100,000 to 1,000,000, capped at the model's context window), so
compaction cannot start before about 67,000 tokens; `/autocompact` and per-model `modelSettings`
can still override it. A `CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment variable overrides every
setting, so `init` warns when one is set. The A/B harness uses that variable on purpose, so an
arm's window cannot be changed by your settings. `simulate` and `doctor` print the setting for
their best policy.

## See it live

Add CimriHook's status line to Claude Code (`~/.claude/settings.json`):

```json
"statusLine": {"type": "command", "command": "cimrihook statusline"}
```

`412k ctx · cache warm 38m · next $0.08 · 5h 42% · 7d 18% · $4.12`

It shows the session's context, whether the prompt cache is still warm and for how long, what
the next request costs at list prices (a cache read while warm, the re-cache Claude Code expects
once cold), your 5-hour and 7-day usage limits and the session's spend. Cache warmth, lifetime and
expiry come from Claude Code's own `prompt_cache` input (2.1.251 and later); Claude Code also
re-runs the status line when the cache expires. Every new usage-limit reading is recorded in the
ledger, so CimriHook can learn how your plan counts cache reads, writes and output.

`cimrihook init` keeps a status line you already have: it runs first with the same input, every
line of its output is kept and CimriHook's part ends the last line. If CimriHook's part fails, its
error is shown in the line instead (Claude Code blanks the whole status line on a non-zero exit).

## Evaluate with your real subscriptions

```bash
cimrihook bench-run --name claude-ablation --agents claude --protocols sequential \
  --variants baseline,governor,codec,combined --window 100000 --reps 5
cimrihook bench-run --name codex-governor --agents codex --protocols sequential \
  --variants baseline,governor --window 60000 --reps 5
cimrihook bench-run --name claude-deep --agents claude --protocols deep \
  --variants baseline,governor,brief --window 183000 --reps 5
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
| `brief` | window and the compaction brief (PreCompact) | not available |

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
  so the context accumulates the way it does in real work. The `deep` protocol (Claude Code only)
  first has the agent read every library source file, so the session starts at about 200k tokens
  of context that mostly goes stale: the regime where most real spend happens.
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
cimrihook doctor                                # see where the spend goes first
cimrihook init --dry-run                        # show the change to ~/.claude/settings.json
cimrihook init                                  # cold-prompt guard and status line
cimrihook init --compact-window 183000          # also compact earlier (value from doctor)
cimrihook init --remove                         # take out only what CimriHook added
```

`init` backs the settings file up before writing, keeps the file's permissions, and hides the
values of `env` and `headers` entries in the diff it prints. It is declarative: it first takes
out what an earlier `init` added, then adds the parts you selected now, so re-running it after an
upgrade or a move replaces the old commands instead of adding new ones next to them. A status line
you already have is chained (its command is kept in CimriHook's `--after` argument, which is also
where `--remove` restores it from), and the environment values it replaced are remembered per
settings file in `~/.cimrihook/installed.json` so `--remove` can put them back. `--brief` and
`--codec` add the compaction brief and the tool codec. `cimrihook settings` prints the same blocks
for merging by hand or for a single run: `claude --settings "$(cimrihook settings)"`.

The hook and status line commands run Python with `-I`: they run in your project directory,
outside Claude Code's permission prompts, and without `-I` a `json.py` or `statistics.py` in that
directory would run in CimriHook's place. Everything CimriHook writes under `~/.cimrihook` is
readable by you only. After changing the source of an installed copy, reinstall it with
`uv tool install --force --reinstall --refresh /path/to/CimriHook`: the version number does not
change, so uv would otherwise reuse the old build.

## Guard the cache

When a session sits idle past its prompt-cache lifetime (1 hour on subscriptions, 5 minutes
elsewhere), the next message re-caches the whole conversation. On a 900k-token session that is
one request of about $7 at list prices. The cold-prompt guard (`UserPromptSubmit` hook) stops that
message once, says what it would cost and suggests `/compact`; sending the message again goes
ahead. It only steps in above `CIMRIHOOK_GUARD_MIN_TOKENS` of context, reads the cache lifetime
from the last cache write in the transcript, stays quiet right after a compaction (the context is
small again), and never blocks on its own errors: they exit 1, which Claude Code shows without
stopping the prompt. `cimrihook doctor` ends with a check that the guard can read your newest
session.

A stopped prompt can only be sent again by a person. Claude Code 2.1.288 does not tell hooks
where a prompt came from, so the guard never stops prompts that are clearly not typed by you:
system notifications (`[SYSTEM NOTIFICATION ...]`), tagged messages such as
`<task-notification>`, commands starting with `/` (including `/compact` and `/loop`) and subagent
prompts. A scheduled prompt that arrives as plain text cannot be told apart from yours; if you
deliver such prompts into long idle sessions, switch the guard off with
`CIMRIHOOK_DISABLE=guard`.

`--brief` adds a `PreCompact` hook whose output Claude Code appends to its compaction prompt
(every compaction path does this in 2.1.288, though the hooks reference does not document it):
keep the summary short and structured, refer to code by file path and line instead of pasting it.
Claude Code's own nine-section summary template still dominates, and Claude Code shows the hook's
output to you at every compaction. A summary is a small part of the context that follows it, so
expect little cost effect; in the one deep pilot run it was within noise of the plain window. It
stays opt-in.

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
| `CIMRIHOOK_DISABLE` | empty | comma list of `guard,ref,delta,outline` to switch parts off |
| `CIMRIHOOK_GUARD_MIN_TOKENS` | `150000` | the cold-prompt guard only stops sessions at least this large |
| `CIMRIHOOK_OUTLINE_MIN_TOKENS` | `6000` | outline threshold |
| `CIMRIHOOK_DELTA_MAX_RATIO` | `0.5` | send a delta only if it is at most this share of the raw result |
| `CIMRIHOOK_MIN_SAVING_TOKENS` | `150` | do not re-encode for smaller savings |

## Limitations

- When a Bash command exits non-zero, hooks only receive a plain-text error
  (`PostToolUseFailure`), which they cannot rewrite. Failing test runs pass through unchanged.
- Claude Code can clear old tool results in memory (microcompaction) without writing a marker. If a
  REF or DELTA points at text the model can no longer see, the model repeats the call and gets the
  raw result.
- With `--codec`, the ledger keeps the text the agent received (zlib-compressed, which is not
  encryption) in `~/.cimrihook/ledger.sqlite3`, readable by you only and not deleted
  automatically. Claude Code's own transcripts store the same text.
- Token numbers in reports are estimates (~4 characters per token). Evaluations should use the
  usage numbers reported by the API.

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```
