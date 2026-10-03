"""Tasarruf tablolarının metin çıktısı (canlı rapor ve audit ortak kullanır)."""

from collections.abc import Sequence

from cimrihook.model import SavingsRow


def render_table(rows: Sequence[SavingsRow]) -> list[str]:
    """Kodlama/araç başına sonuç sayısı ve token muhasebesi tablosu."""
    header = (
        f"  {'encoding':<9}{'tool':<6}{'results':>9}{'raw tokens':>14}{'sent tokens':>14}"
        f"{'saved':>14}"
    )
    body = [
        f"  {row.encoding.value:<9}{row.tool:<6}{row.results:>9,}{row.tokens_raw:>14,}"
        f"{row.tokens_sent:>14,}{row.tokens_raw - row.tokens_sent:>14,}"
        for row in rows
    ]
    return [header, *body]


def total_line(rows: Sequence[SavingsRow]) -> str:
    """Tüm tablonun özeti."""
    raw = sum(row.tokens_raw for row in rows)
    saved = raw - sum(row.tokens_sent for row in rows)
    share = 100 * saved / raw if raw > 0 else 0.0
    return (
        f"  saved {saved:,} of {raw:,} tool-result tokens ({share:.1f}%, ~4 chars/token estimate)"
    )


def render_savings(title: str, rows: Sequence[SavingsRow]) -> str:
    """Canlı hook kararlarının raporu."""
    return "\n".join([title, *render_table(rows), total_line(rows)])
