"""Claude Code ayar blokları: CimriHook hook'ları ve bağlam yöneticisi."""

import shlex
from typing import Final

HOOK_MATCHER: Final = "Read|Bash|Edit|Write|MultiEdit|NotebookEdit"
HOOK_TIMEOUT_SECONDS: Final = 10


def hook_settings(python: str) -> dict[str, object]:
    """Claude Code settings.json'a eklenecek hooks bloğu (verilen Python yorumlayıcısıyla)."""
    handler = {
        "type": "command",
        "command": f"{shlex.quote(python)} -m cimrihook hook",
        "timeout": HOOK_TIMEOUT_SECONDS,
    }
    return {
        "hooks": {
            "SessionStart": [{"hooks": [handler]}],
            "PreCompact": [{"hooks": [handler]}],
            "PostToolUse": [{"matcher": HOOK_MATCHER, "hooks": [handler]}],
        }
    }


def governor_env(window: int) -> dict[str, object]:
    """Bağlam yöneticisi: Claude Code'un kendi otomatik sıkıştırma penceresini daraltır.

    Maliyetin büyük kısmı her istekte yeniden okunan bağlamdan geldiği için pencereyi küçültmek
    en büyük kaldıraçtır; uygun pencere `cimrihook simulate` ile kullanıcının verisinden seçilir.
    """
    return {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window)}}
