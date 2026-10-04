"""CimriHook'un Claude Code mod'u: paketteki dosyalardan eklenti klasörünü kurar.

Mod, Claude Code'un fonksiyon hook'larıdır (2.1.286 ve sonrası; masaüstü uygulaması dahil):
sıkıştırmayı önbellek ömrüne göre zamanlar (bkz. mod/register.ts). Eklenti üç dosyadır:
.claude-plugin/plugin.json, hooks/hooks.json ve hooks/register.ts. Paket bunları düz bir
klasörde taşır, kurulum eklentinin düzenini oluşturur. Claude Code klasörü
CLAUDE_CODE_PLUGIN_DIRS'ten yükler; değişken kullanıcı ayarlarının env bloğundan da okunur,
masaüstü uygulamasının başlattığı oturumlar dahil.
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
    """Mod'un kurulduğu eklenti klasörü."""
    return home / "mod" / MOD_NAME


def write_mod(target: Path) -> Path:
    """Paketteki mod dosyalarını eklenti düzeniyle yazar (varsa üzerine) ve klasörü döndürür."""
    source = resources.files("cimrihook") / "mod"
    for relative, name in LAYOUT:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target
