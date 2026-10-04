"""CimriHook's Claude Code mod: installs the plugin folder from the files in the package.

The mod is a set of Claude Code function hooks (2.1.286 and later; the desktop app included):
it schedules compaction around the cache lifetime (see mod/register.ts). The plugin is three
files: .claude-plugin/plugin.json, hooks/hooks.json and hooks/register.ts. The package keeps
them in a flat folder and the installation builds the plugin layout. Claude Code loads the
folder from CLAUDE_CODE_PLUGIN_DIRS; the variable is also read from the env block of the user
settings, including for sessions the desktop app starts.
"""

from importlib import resources
from pathlib import Path
from typing import Final

PLUGIN_DIRS_ENV: Final = "CLAUDE_CODE_PLUGIN_DIRS"
MOD_NAME: Final = "cimrihook"
LAYOUT: Final = (
    (".claude-plugin/plugin.json", "plugin.json"),
    ("hooks/hooks.json", "hooks.json"),
    ("hooks/register.ts", "register.ts"),
)


def mod_dir(home: Path) -> Path:
    """The plugin folder the mod is installed into."""
    return home / "mod" / MOD_NAME


def write_mod(target: Path) -> Path:
    """Writes the packaged mod files in the plugin layout (overwriting) and returns the folder."""
    source = resources.files("cimrihook") / "mod"
    for relative, name in LAYOUT:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target
