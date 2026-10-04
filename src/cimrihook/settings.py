"""Claude Code settings blocks: the CimriHook components and the context governor.

Each component produces its own block and the blocks are merged per event. A component thus never
registers another one's hook twice, and ablation experiments can switch components on separately.
"""

import shlex
from collections.abc import Sequence
from typing import Final

HOOK_TIMEOUT_SECONDS: Final = 10


def command(python: str, subcommand: str) -> str:
    """Shell command that runs a CimriHook subcommand with the given Python interpreter.

    Hooks and the status line run outside Claude Code's permission prompts, in the session's working
    directory. `-I` keeps the interpreter from adding that directory (and PYTHON* variables) to the
    import path; without it a project's `json.py` or `statistics.py` would run in CimriHook's place.
    """
    return f"{shlex.quote(python)} -I -m cimrihook {subcommand}"


def handler(python: str, subcommand: str) -> dict[str, object]:
    """A command-type hook definition."""
    return {
        "type": "command",
        "command": command(python, subcommand),
        "timeout": HOOK_TIMEOUT_SECONDS,
    }


def guard_settings(python: str) -> dict[str, object]:
    """Cold-prompt guard: asks once before a prompt into a large session whose cache went cold."""
    return {"hooks": {"UserPromptSubmit": [{"hooks": [handler(python, "guard")]}]}}


def statusline_settings(python: str) -> dict[str, object]:
    """Status line: context, cache warmth, the next request's cost and the usage limits."""
    return {"statusLine": {"type": "command", "command": command(python, "statusline")}}


def chained_statusline_command(ours: str, previous: str) -> str:
    """CimriHook status line command that first runs the user's previous status line command."""
    return f"{ours} --after {shlex.quote(previous)}"


def governor_settings(window: int) -> dict[str, object]:
    """Context governor: Claude Code's own auto-compaction window setting.

    Most of the cost comes from the context re-read on every request, so shrinking the window is
    the biggest lever; `cimrihook doctor` picks a suitable window from the user's own data. Claude
    Code 2.1.288 caps `autoCompactWindow` (100000-1000000) at the model's window and triggers
    compaction at the window − 33000 of context. Unlike the environment variable, /autocompact and
    per-model settings (modelSettings) can override this value.
    """
    return {"autoCompactWindow": window}


def cache_ttl_settings(main: str | None, subagent: str | None) -> dict[str, object]:
    """Cache lifetimes: main conversation (promptCacheTtl) and subagents (subagentPromptCacheTtl).

    Claude Code 2.1.242 and later; the value is "5m" or "1h". A 1-hour write costs 2x the input
    price and a 5-minute write 1.25x, but if the pause outlasts the lifetime the next request writes
    the whole context again; `cimrihook doctor` works out which is cheaper from the user's pauses.
    """
    return {
        **({} if main is None else {"promptCacheTtl": main}),
        **({} if subagent is None else {"subagentPromptCacheTtl": subagent}),
    }


def mod_settings(plugin_dir: str) -> dict[str, object]:
    """CimriHook mod: added to the Claude Code plugin folders (CLAUDE_CODE_PLUGIN_DIRS).

    The installation keeps the folders the user already has in this variable and adds the mod last.
    """
    return {"env": {"CLAUDE_CODE_PLUGIN_DIRS": plugin_dir}}


def governor_env(window: int) -> dict[str, object]:
    """Context governor through the environment variable CLAUDE_CODE_AUTO_COMPACT_WINDOW.

    It overrides every setting, so the A/B harness uses it: an arm's window is not affected by user
    or project settings.
    """
    return {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window)}}


def merge_settings(blocks: Sequence[dict[str, object]]) -> dict[str, object]:
    """Merges settings blocks: hooks are appended per event, env entries merged per key."""
    hooks: dict[str, list[object]] = {}
    env: dict[str, object] = {}
    rest: dict[str, object] = {}
    for block in blocks:
        for key, value in block.items():
            if key == "hooks" and isinstance(value, dict):
                for event, entries in value.items():
                    if isinstance(entries, list):
                        hooks[str(event)] = [*hooks.get(str(event), []), *entries]
            elif key == "env" and isinstance(value, dict):
                env = env | {str(name): item for name, item in value.items()}
            else:
                rest[key] = value
    return rest | ({"hooks": hooks} if hooks else {}) | ({"env": env} if env else {})
