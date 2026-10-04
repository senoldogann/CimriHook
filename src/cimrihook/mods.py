"""CimriHook's Claude Code mod: installs the plugin folder from the files in the package.

The mod is a set of Claude Code function hooks (2.1.286 and later; the desktop app included):
it schedules compaction around the cache lifetime (see mod/register.ts). The plugin includes
an opt-in closure probe alongside its hooks and metadata. The package keeps
them in a flat folder and the installation builds the plugin layout. Claude Code loads the
folder from CLAUDE_CODE_PLUGIN_DIRS; the variable is also read from the env block of the user
settings, including for sessions the desktop app starts.

The function hooks API is early and changes between Claude Code releases, so the mod is only
installed after `claude plugin validate` accepts it: that checks every hook and `$` call the mod
makes against the installed build. The governor and guard are plain settings and do not depend
on it.
"""

import subprocess
import tempfile
from importlib import resources
from pathlib import Path
from typing import Final

from cimrihook.errors import ConfigError

PLUGIN_DIRS_ENV: Final = "CLAUDE_CODE_PLUGIN_DIRS"
MOD_NAME: Final = "cimrihook"
VALIDATE_TIMEOUT_SECONDS: Final = 60
LAYOUT: Final = (
    (".claude-plugin/plugin.json", "plugin.json"),
    ("hooks/hooks.json", "hooks.json"),
    ("hooks/register.ts", "register.ts"),
    ("hooks/closure.ts", "closure.ts"),
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


def validate_mod() -> str:
    """Checks the packaged mod against the installed Claude Code; its report, or a ConfigError.

    The mod is laid out in a temporary folder, so nothing is written for a refused mod or a
    dry run.
    """
    with tempfile.TemporaryDirectory(prefix="cimrihook-mod-check-") as directory:
        plugin = write_mod(Path(directory))
        command = ["claude", "plugin", "validate", str(plugin)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=VALIDATE_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as error:
            raise ConfigError(
                "the mod needs the claude CLI on PATH to check it against the installed Claude "
                "Code; install without --mod to keep the governor and guard only"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ConfigError(
                f"{' '.join(command)} did not finish in {VALIDATE_TIMEOUT_SECONDS} s"
            ) from error
    report = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise ConfigError(
            f"this Claude Code does not accept the mod (claude plugin validate exit "
            f"{result.returncode}); install without --mod to keep the governor and guard only:\n"
            f"{report}"
        )
    return report
