# CimriHook: bağlam ömrü yöneticisi — tasarım

Tarih: 2026-10-03 · Durum: onaylandı, uygulanıyor (aşama 1'den başlayarak)

## 1. Sorun ve içgörü

Kodlama ajanı her API isteğinde o ana kadarki konuşmanın tamamını yeniden okur. Bu yüzden bir
oturumun maliyeti uzunluğuyla **karesel** büyür. 1M bağlamlı modellerde otomatik sıkıştırma ~950k
tokenda devreye girdiğinden karesel terim çok büyüyebilir.

Kullanıcının son 7 günlük gerçek kayıtları (API liste fiyatıyla $2 435):

| Gözlem | Değer |
|---|---|
| Bağlamı >200k olan isteklerin maliyet payı | %81 (>400k: %57) |
| Alt ajanların payı | %43 |
| Opus'ta büyük cache yeniden yazımları | Opus maliyetinin %15.7'si (>1 saat boşluk: %8.3) |
| Statik önek (ilk istek) | ana oturum 34.6k, alt ajan 23.9k token |
| Kayıpsız codec (REF/DELTA) potansiyeli | tool çıktılarının %0.39'u |

RTK yeni içeriği (doğrusal terim) küçültür. Faturanın asıl kalemi ise birikmiş geçmişin her
istekte yeniden okunması (karesel terim) ve cache soğuduğunda tüm bağlamın yeniden yazılmasıdır.
A/B ölçümleri bunu doğruluyor: yalnızca sıkıştırma penceresi Codex'te ~%20, Claude'da ~%5
(küçük bağlamlı görevlerde) tasarruf etti; codec ise hiç tetiklenmedi.

## 2. Ürün tanımı

> CimriHook, Claude Code ve Codex için bir **bağlam ömrü yöneticisidir**: oturum maliyetini
> karesel olmaktan çıkarır, cache'i sıcak tutar ve kullanım limitini görünür kılar.
> RTK içeriye gireni küçültür; CimriHook ne kadar kalacağını ve ne zaman tazeleneceğini yönetir.

Kullanıcı deneyimi (RTK benzeri: tek kurulum, görünür kazanç, iş akışı değişmez):

| Komut / yüzey | Ne yapar |
|---|---|
| `cimrihook doctor` | Kendi kayıtlarından "paran nereye gidiyor": bağlam bandları, soğuk yeniden yazımlar ve nedenleri, statik önek, alt ajanlar, sıkıştırmalar; önerilen değişiklikler ve simülasyon tahmini (kalibrasyon hatasıyla birlikte) |
| `cimrihook init` | Tek komutla kurulum (yedekli, `--dry-run`): model bazlı sıkıştırma pencereleri, hook'lar, durum satırı; Codex için config değerleri. `--remove` geri alır |
| Durum satırı | Bağlam boyutu, cache sıcaklığı ve kalan süresi, sonraki isteğin tahmini maliyeti, 5 saatlik ve 7 günlük kota |
| Korumalar | Soğuk istem: cache süresi dolmuş büyük bir oturuma istem gönderilirken bir kez durdurur ve maliyeti söyler. Soğuk devam ve model değişimi notları |
| `cimrihook gain` | Kurulumdan önce ve sonra gerçekleşen maliyet (kendi kayıtlarından) |
| `cimrihook quota` | Planın kullanım limitinin token türlerini nasıl saydığını durum satırı örneklerinden öğrenir |

## 3. Bileşenler ve platform yüzeyleri

Claude Code 2.1.288 binary'sinde ve Codex CLI 0.160'ta doğrulandı.

| Bileşen | Mekanizma | Yüzey |
|---|---|---|
| Governor | Model bazlı `autoCompactWindow` (100k–1M; tetik = pencere − 33k), Codex `model_auto_compact_token_limit` | Ayar |
| Soğuk istem koruması | `UserPromptSubmit` → `decision: "block"` + `reason`. Aynı istem kısa süre içinde tekrar gönderilirse geçer (fail-open) | Hook (Claude ve Codex) |
| Soğuk devam notu | `SessionStart` resume/fork alanları: `prompt_cache_likely_expired`, `context_tokens`, `estimated_cache_write_usd` | Hook |
| Sıkıştırma özeti | `PreCompact` stdout'u özel compact talimatı olur: bütçeli, yapılandırılmış özet | Hook (yardım metninde var, web belgelerinde yok: orta risk) |
| Çalışma seti | `SessionStart` (source=compact) `additionalContext`: düzenlenen dosyalar, son test komutu ve sonucu | Hook |
| Durum satırı + kota örnekleyici | statusline JSON: `context_window`, `cost`, `rate_limits.five_hour/seven_day.used_percentage` | Statusline komutu |
| Codec (isteğe bağlı) | Mevcut `PostToolUse` REF/DELTA/OUTLINE; ölçülen değer çoğunlukla alt ajan OUTLINE'ı | Hook |
| Kanıt | Mevcut A/B harness, `bench-calibrate`; yüksek bağlam görevleri (E2) | Araç |

## 4. Mimari

- Mevcut modüller korunur: `claude`, `codec`, `ledger`, `hook`, `settings`, `simulate`, `audit`,
  `bench`, `stats`.
- Yeni modüller, her biri tek amaçlı:
  - `doctor`: maliyet anatomisi, saf fonksiyonlar ve rapor.
  - `statusline`: durum satırı ve kota örneği.
  - `guard`: istem, devam ve sıkıştırma politikaları.
  - `install`: init/remove; yedekli ve idempotent.
  - `quota`: kota ağırlıklarının regresyonu.
- `hook.py` olay adına göre yönlendirir:
  - `PostToolUse` → codec (yalnızca açıksa),
  - `UserPromptSubmit` → soğuk istem koruması,
  - `SessionStart` → kuşak + devam notu + çalışma seti,
  - `PreCompact` → kuşak + sıkıştırma özeti.
- Durum `~/.cimrihook/ledger.sqlite3` içinde tutulur; yeni tablolar: `quota_samples`, `guard_acks`.
- Fail-open: her hata çıkış kodu 1 ile biter. Bu, engellemeyen bir hatadır; ajan özgün davranışla
  devam eder.
- Hiçbir ağ çağrısı yapılmaz; kayıtlar makineden çıkmaz.

## 5. Güvenlik ve kalite

- **Önce danışman:** Korumalar en fazla bir kez durdurur ve nedenini sayılarla söyler; kullanıcı
  tekrar gönderirse geçer.
- **Temkinli varsayılanlar:** Pencere kalibre simülasyondan model bazında seçilir. Ayar 100k'nın
  altına inmez; belgelenmemiş bayraklar kullanılmaz.
- **Kapatma anahtarları:**
  - `CIMRIHOOK_DISABLE=guard,codec,brief,workingset,statusline` (bileşen başına),
  - `cimrihook init --remove`.
- **Ayarlara dokunma kuralı:** Kullanıcı ayarları yalnızca `init` komutuyla değişir; önce yedek
  alınır ve `--dry-run` ile fark gösterilir.
- **Doğrulama:**
  - Simülasyon tahminleri `bench-calibrate` ile ölçülür.
  - Kalite için yüksek bağlam A/B'si yapılır; non-inferiority kararı kol başına en az 5 koşu ister.

## 6. Dil kararı

Python (yalnızca standart kütüphane) kalır.

- Kazanç politikadan gelir, çalıştırma hızından gelmez.
- Hook'lar istem ve sıkıştırma başına çalışır (codec kapalıyken tool başına değil).
- Durum satırı komutu oturum güncellendikçe çalışır; ~70 ms'lik bir Python süreci kabul edilebilir.
  Ölçülüp aşılırsa yalnızca bu yol hızlandırılır.
- Analiz, simülatör ve harness mevcut ve testlidir.
- Taşıma koşulu: Dağıtımda Python bağımlılığı engel olursa, yalnızca çalışma zamanı kısmı (hook +
  durum satırı) tek bir Rust ikilisine taşınır. C++ önerilmez.

## 7. Aşamalar ve kabul ölçütleri

| # | Aşama | Kabul ölçütü |
|---|---|---|
| 1 | `doctor` (salt-okunur) | 7 günlük veride saniyeler içinde çalışır; bant ve kalem toplamları genel toplamla tutar; tahminler "simülasyon, doğrulanmamış" etiketlidir |
| 2 | Durum satırı + kota örnekleyici | Sentetik girdilerle doğru çıktı; eksik alanlarda fail-open; örnekler deftere yazılır |
| 3 | Korumalar (soğuk istem, soğuk devam, sıkıştırma özeti, çalışma seti) | Uçtan uca hook testleri; tekrar gönderimde geçer; hata durumunda engellemez |
| 4 | `init` / `--remove` (Claude ayarları, Codex config) | `--dry-run` farkı, yedek, idempotent; ayarlara yalnızca açık komutla dokunur |
| 5 | `quota` | Yeterli örnekte token türü ağırlıklarını güven aralığıyla raporlar |
| 6 | `gain` | Kurulum öncesi ve sonrası dönemleri aynı ölçüyle karşılaştırır |
| 7 | Yüksek bağlam A/B (E2) | Governor + sıkıştırma özeti: sağlayıcı düzeyinde ölçülmüş tasarruf ve non-inferiority |

## 8. Kapsam dışı

- Abonelik trafiğini proxy üzerinden değiştirmek (sağlayıcı koşulları; tez kısıtı).
- API anahtarıyla sunucu tarafı bağlam düzenleme (sonraki çalışma).
- Oturum içinde modeli otomatik değiştirmek (cache'i bozar; yalnızca uyarı verilir).
