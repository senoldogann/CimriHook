# Local task preparation

## Objective and scope

Complete the same accepted work with less Claude/Codex subscription usage. The user approved
the preparation-first direction and immediate implementation. This first increment combines
an opportunity report, an explicit-target packet and a matched pilot. No model is called by
the report or packet builder. No settings, histories, model or effort are automatically changed.

## Opportunity report

`cimrihook preparation-report --agent claude|codex --days N [--logs-dir PATH] [--json]`
reads recent native JSONL logs. Reuse existing file discovery/parsing conventions. Count recognized
research calls (Read/Glob/Grep and simple read-only shell commands), edits, identical successful
observations for identical calls, and prompt/turn spans with research before the first edit.
Deduplicate native IDs across streaming records and copied histories. Preserve parallel Claude
tool calls as one request batch. Codex call counts are not inferred model request counts.
Unrecognized tools, corrupt lines and missing results remain visible as unknown/incomplete counts.
An identical observation is not proof of redundant reasoning or unchanged files. Do not attribute
quota points, dollar savings or recoverable percentages to these counts. Output aggregates only.

## Explicit preparation packet

`cimrihook prepare --root PATH --request-file PATH --source TARGET ...
[--evidence-file PATH ...] [--max-bytes N] [--json]` preserves the original UTF-8 request.
TARGET is a repository-relative file, `file:start:end` (inclusive lines), or `file::symbol`
for an explicitly named Python function/class, including qualified nested names. Use AST locations
only, never model-generated summaries or guessed dependencies. Every source includes its full-file
SHA-256, selected line range, numbered literal content, and whether that is the entire file.
Evidence files are literal observations, not claims that a current test passed. Include Git HEAD
and literal porcelain status, and require a consistent snapshot before/after assembly.
Reject outside-root/symlink escapes, invalid ranges, missing/ambiguous symbols, undecodable files,
overlarge inputs and packets. Never silently trim. The limit applies to the actual rendered UTF-8
packet, including request and metadata; JSON transport size is also bounded when selected.
No command from the request, evidence or sources is executed. Keep all preparation local.

## Matched pilot

Extend the existing benchmark with `targeted-governor` and `prepared-governor` arms for Claude and
Codex, single/sequential protocols only. Both receive the same explicit source path/symbol scope
and the same failing test observation. Only the prepared arm receives literal source content and
snapshot metadata. Scope must be declared in task JSON independently of mutation replacements;
never derive a target by locating the injected edit. Save each exact submitted prompt and packet.
Use existing test setup, isolation, fixed model/effort/window, provider counters and untouched-test
verification. Pilot-specific Claude limits: 12 model turns and $3 per generation call, with bounded
wall time. Record failed attempts; do not report only successful costs. Test outcomes and model
requests are separate from coarse account-wide quota observations. A small screening pair is
feasibility evidence, not a general savings or quality claim. Existing dependent-task coverage is
not sufficient: dependent-refactor acceptance remains a gate before broad product recommendation.

## Acceptance

Real-file/process smoke tests cover both log formats, copied calls, successful/failed observations,
explicit source selection, snapshot integrity and CLI errors. Ruff, format, strict mypy and the
existing Python suite pass. Run local reports and a bounded matched screening pilot in an isolated
fixture. Publish aggregate counts, exact pilot scope, quality results and costs without private text.
An unsuccessful or more expensive pilot remains opt-in and does not change defaults.
