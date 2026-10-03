"""CimriHook'un tipli veri modelleri."""

from dataclasses import dataclass
from enum import StrEnum


class Encoding(StrEnum):
    """Bir araç sonucunun modele hangi biçimde iletildiği."""

    RAW = "raw"  # sonuç olduğu gibi iletildi; sonraki deltalar için anahtar kare olur
    REF = "ref"  # içerik bağlamda birebir mevcut; kısa referans gönderildi
    DELTA = "delta"  # son anahtar kareye göre birleşik fark gönderildi
    OUTLINE = "outline"  # büyük dosyanın yerine yapısal iskelet gönderildi


class Tool(StrEnum):
    """Sonucu yeniden kodlanabilen Claude Code araçları."""

    READ = "Read"
    BASH = "Bash"


@dataclass(frozen=True, slots=True)
class Observation:
    """Bir araç sonucunun araçtan bağımsız, normalize edilmiş hali."""

    tool: Tool
    stream: str  # aynı bilgi akışının kimliği (gerçek dosya yolu ya da çalışma dizini + komut)
    request_key: str  # isteğin birebir kimliği; kaçış kuralı bununla eşleşir
    label: str  # Read için ajanın verdiği dosya yolu, Bash için kısaltılmış komut
    start_line: int  # ilk satırın akıştaki numarası (1 tabanlı)
    lines: tuple[str, ...]
    total_lines: int  # akışın toplam satır sayısı
    whole: bool  # sonuç akışın tamamını kapsıyor mu
    full_request: bool  # yalnızca Read için: istek offset/limit vermeden mi yapıldı


@dataclass(frozen=True, slots=True)
class View:
    """Defter kaydı: ajanın belirli bir adımda bir akıştan edindiği bilgi."""

    step: int
    stream: str
    request_key: str
    encoding: Encoding
    start_line: int
    lines: tuple[str, ...]  # OUTLINE için boş: ajan içeriği görmedi
    total_lines: int
    whole: bool


@dataclass(frozen=True, slots=True)
class Decision:
    """Codec kararı ve tahmini token muhasebesi."""

    encoding: Encoding
    message: str  # RAW için boş
    tokens_raw: int  # ham sonucun tahmini token maliyeti
    tokens_sent: int  # modele gerçekten giden tahmini token


@dataclass(frozen=True, slots=True)
class QuotaSample:
    """Abonelik kullanım limitinin bir anlık gözlemi (Claude Code durum satırı girdisinden)."""

    window: str  # five_hour ya da seven_day
    resets_at: int  # pencerenin sıfırlandığı an (epoch saniye)
    used_percentage: float
    taken_at: float
    session_id: str
    model: str


@dataclass(frozen=True, slots=True)
class SavingsRow:
    """Bir kodlama + araç çifti için toplam token muhasebesi."""

    encoding: Encoding
    tool: str
    results: int
    tokens_raw: int
    tokens_sent: int
