"""Claude Code ayar blokları: CimriHook bileşenleri ve bağlam yöneticisi.

Her bileşen kendi bloğunu üretir; bloklar olay başına birleştirilir. Böylece bir bileşen diğerinin
hook'unu ikinci kez kaydetmez ve ablasyon deneylerinde bileşenler ayrı ayrı açılabilir.
"""

import shlex
from collections.abc import Sequence
from typing import Final

HOOK_MATCHER: Final = "Read|Bash|Edit|Write|MultiEdit|NotebookEdit"
HOOK_TIMEOUT_SECONDS: Final = 10


def command(python: str, subcommand: str) -> str:
    """CimriHook alt komutunu verilen Python yorumlayıcısıyla çalıştıran kabuk komutu.

    Hook'lar ve durum satırı Claude Code'un izin istemlerinin dışında, oturumun çalışma dizininde
    çalışır. `-I` yorumlayıcının o dizini (ve PYTHON* değişkenlerini) içe aktarma yoluna katmasını
    engeller; yoksa projedeki bir `json.py` ya da `statistics.py` CimriHook'un yerine çalışırdı.
    """
    return f"{shlex.quote(python)} -I -m cimrihook {subcommand}"


def handler(python: str, subcommand: str) -> dict[str, object]:
    """Komut tipindeki hook tanımı."""
    return {
        "type": "command",
        "command": command(python, subcommand),
        "timeout": HOOK_TIMEOUT_SECONDS,
    }


def hook_settings(python: str) -> dict[str, object]:
    """Codec hook'ları: araç sonuçlarını yeniden kodlar, bağlam kuşaklarını izler."""
    codec = handler(python, "hook")
    return {
        "hooks": {
            "SessionStart": [{"hooks": [codec]}],
            "PreCompact": [{"hooks": [codec]}],
            "PostToolUse": [{"matcher": HOOK_MATCHER, "hooks": [codec]}],
        }
    }


def guard_settings(python: str) -> dict[str, object]:
    """Soğuk istem koruması: önbelleği soğumuş büyük oturuma istemden önce bir kez sorar."""
    return {"hooks": {"UserPromptSubmit": [{"hooks": [handler(python, "guard")]}]}}


def brief_settings(python: str) -> dict[str, object]:
    """Sıkıştırma özeti: Claude Code'un özetleme isteğine kısa ve yapılandırılmış özet talimatı."""
    return {"hooks": {"PreCompact": [{"hooks": [handler(python, "brief")]}]}}


def statusline_settings(python: str) -> dict[str, object]:
    """Durum satırı: bağlam, önbellek sıcaklığı, sonraki isteğin maliyeti ve kullanım limitleri."""
    return {"statusLine": {"type": "command", "command": command(python, "statusline")}}


def chained_statusline_command(ours: str, previous: str) -> str:
    """Kullanıcının önceki durum satırı komutunu önce çalıştıran CimriHook durum satırı komutu."""
    return f"{ours} --after {shlex.quote(previous)}"


def governor_settings(window: int) -> dict[str, object]:
    """Bağlam yöneticisi: Claude Code'un kendi otomatik sıkıştırma penceresi ayarı.

    Maliyetin büyük kısmı her istekte yeniden okunan bağlamdan geldiği için pencereyi küçültmek
    en büyük kaldıraçtır; uygun pencere `cimrihook doctor` ile kullanıcının verisinden seçilir.
    Claude Code 2.1.288 `autoCompactWindow` değerini (100000-1000000) modelin penceresiyle sınırlar
    ve sıkıştırmayı pencere − 33000 bağlamda tetikler. Ortam değişkeninin aksine /autocompact ve
    model başına ayarlar (modelSettings) bu değeri geçersiz kılabilir.
    """
    return {"autoCompactWindow": window}


def governor_env(window: int) -> dict[str, object]:
    """Bağlam yöneticisi, ortam değişkeniyle: CLAUDE_CODE_AUTO_COMPACT_WINDOW her ayarı ezer.

    A/B düzeneği bunu kullanır: kolun penceresi kullanıcı ya da proje ayarlarından etkilenmez.
    """
    return {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window)}}


def merge_settings(blocks: Sequence[dict[str, object]]) -> dict[str, object]:
    """Ayar bloklarını birleştirir: hook'lar olay başına eklenir, env anahtar başına birleşir."""
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
