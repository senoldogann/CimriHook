"""CimriHook'a özgü hata tipleri."""


class CimriHookError(Exception):
    """Tüm CimriHook hatalarının tabanı."""


class HookPayloadError(CimriHookError):
    """Claude Code hook yükü ya da araç sonucu beklenen şemaya uymuyor."""


class ConfigError(CimriHookError):
    """Ortam değişkeni yapılandırması geçersiz."""


class BenchError(CimriHookError):
    """Değerlendirme düzeneği kurulamadı ya da bir çalıştırma ölçülemedi."""
