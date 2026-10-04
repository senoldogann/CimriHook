# Local task preparation implementation plan

> **For agentic workers:** Use executing-plans for tightly coupled inline execution. Steps use checkbox syntax.

**Goal:** Ship local preparation diagnostics, explicit task packets and an opt-in matched screening pilot.

**Architecture:** Separate native-log analysis from immutable packet construction. Integrate both into
the existing CLI and reuse the existing benchmark runner for paired explicit-target arms.

**Tech Stack:** Python 3.12 standard library, existing pytest/ruff/mypy toolchain, Claude/Codex CLIs.

## Task 1: Native log opportunity analysis

Files: create `src/cimrihook/preparation_report.py`; create `tests/test_preparation.py`;
modify `src/cimrihook/cli.py` and `src/cimrihook/errors.py`.

- [ ] Parse Claude tool_use/result and Codex response_item function/custom tool calls from real files.
- [ ] Deduplicate stable call IDs and count recognized research, edits and identical successful results.
- [ ] Track research-before-edit spans; expose unknown calls, missing results and malformed records.
- [ ] Add preparation-report parser and text/JSON dispatch; aggregate without private contents.
- [ ] Test synthetic native records in real temporary files, then run both local seven-day reports.

## Task 2: Immutable explicit task packet

Files: create `src/cimrihook/preparation.py`; extend `tests/test_preparation.py` and CLI.

- [ ] Define frozen SourceTarget/SourceExcerpt/Evidence/PreparationPacket records.
- [ ] Resolve relative paths inside root, literal ranges and explicit Python qualified symbols.
- [ ] Preserve request and evidence; record full-file hashes, literal source and Git snapshot.
- [ ] Recheck snapshot and hashes; reject changes, missing targets, invalid encoding and byte overflow.
- [ ] Render bounded text/JSON packets; add prepare command with repeatable source/evidence arguments.
- [ ] Verify CLI against an actual temporary Git repository and changed/outside-root files.

## Task 3: Paired pilot integration

Files: modify `src/cimrihook/bench.py`, `tests/test_bench_records.py`, `README.md`;
create `bench/tasks/preparation-smoke.json` with a pinned fixture and explicit target scopes.

- [ ] Extend task schema with optional explicit preparation targets, validated without mutation lookup.
- [ ] Add targeted-governor/prepared-governor to window and Codex variants; reject unsupported protocols.
- [ ] Give both arms identical request, target scope and observed failure; only prepared gets sources.
- [ ] Archive submitted prompts/packets per step; enforce a 12-turn/$3 ceiling for these Claude arms.
- [ ] Run a small interleaved screening pair with the same model, effort and window and host verification.
- [ ] Record costs, requests, test results and excluded attempts. Do not infer subscription saving.

## Task 4: Review and verification

- [ ] Review spec/plan before implementation and review implementation for acceptance and correctness.
- [ ] Run `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest -q`.
- [ ] Document usable commands and experimental limits in README and concise aggregate pilot evidence.
- [ ] Inspect the final diff and report results, practical limits and next acceptance gate.
