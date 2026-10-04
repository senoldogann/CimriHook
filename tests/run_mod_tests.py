"""Paketlenmiş plugin'in hook'larını gerçek Claude test engine'i ile kontrol et."""

import subprocess
import tempfile
from pathlib import Path

from cimrihook.mods import write_mod


def main() -> int:
    """Geçici plugin düzeni; model çağrısı veya kullanıcı ayarı değişikliği yok."""
    with tempfile.TemporaryDirectory(prefix="cimrihook-mod-test-") as directory:
        plugin = write_mod(Path(directory))
        for test in (Path(__file__).parent / "mod").glob("*.test.ts"):
            source = test.read_text(encoding="utf-8").replace(
                "../../src/cimrihook/mod/closure", "./closure"
            )
            (plugin / "hooks" / test.name).write_text(source, encoding="utf-8")
        return subprocess.run(["claude", "plugin", "test", str(plugin)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
