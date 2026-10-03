"""Simülatörün maliyet muhasebesi saf bir dönüşümdür; elle hesaplanmış küçük bir oturumla test."""

from cimrihook.audit import Usage
from cimrihook.simulate import OBSERVED, CostModel, Policy, PriceSheet, SessionTrace, simulate_trace

PRICES = PriceSheet("test", read=0.1, write_5m=1.25, write_1h=2.0, uncached=1.0, output=5.0)
MODEL = CostModel(
    write_weight=2.0,
    post_compact_tokens=60,
    post_compact_cached=40,
    summary_tokens=10,
    refetch_tokens=0,
    refetch_requests=0,
)
# İstek 1: 100 token ilk kez yazılır (soğuk). İstek 2: 100 okunur, 20 yazılır (bağlam 120).
TRACE = SessionTrace(
    requests=(Usage(0, 100, 0, 0, 0), Usage(0, 20, 0, 100, 0)),
    model="test",
    prices=PRICES,
    pre_compact_tokens=(),
    post_compact_tokens=(),
    post_compact_cached=(),
    summary_tokens=(),
)


def test_observed_replay_charges_the_logged_reads_and_writes() -> None:
    # 100 x 2.0 + (20 x 2.0 + 100 x 0.1)
    assert simulate_trace(TRACE, OBSERVED, MODEL, PRICES) == (250.0, 0, 220)


def test_compaction_rewrites_only_the_part_that_is_not_still_cached() -> None:
    # İstek 2'den önce 110'u aşan bağlam sıkıştırılır: özetleme 120 x 0.1 okur ve 10 x 5 üretir;
    # yeni bağlamın (60) önbellekte kalan 40'ı okunur, kalan 20'si yazılır: 200 + 62 + 40 + 4.
    assert simulate_trace(TRACE, Policy("p", 110, None), MODEL, PRICES) == (306.0, 1, 160)
