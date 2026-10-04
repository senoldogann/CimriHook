# Doğrulanan görev geçmişini kapatma: ilk deney

## Amaç ve onaylanan kapsam

Aynı kaliteyle abonelik limitinden daha fazla iş çıkarmak. İlk teslimat, gerçek
provider limit okumaları ve görev geçmişini kapatmanın cache/resume davranışını
ölçen opt-in bir Claude pilotudur. Model ve effort sabit kalır. Başarılı fizibilite
sonucu bile bütün iş türlerinde kalite veya tasarruf kanıtı sayılmaz.

## Mevcut durum

Governor'ın karşılaştırma noktası 183000 penceresidir. Mask/codec/brief deneyleri
ek tasarruf kanıtlamamıştır. Modun mesaj handle'larını koruması geçmişte resume
sırasında eski geçmişin geri gelmesine yol açmıştır. Bu nedenle üretimde otomatik
görev kapatma açılmayacak; önce gerçek engine üzerinde kontrol edilecektir.

## T3 Code araştırması

İncelenen kaynak: `pingdotgg/t3code`,
`4ee6bfd50ef4a089440d5c3662db2298da9cc50e`.

- `usage/UsageService.ts`: transcript token kayıtlarından API eşdeğeri maliyet.
- `provider/Layers/claudeUsageLimits.ts`: `get_usage` 0–100 utilization;
  `rate_limit_event` 0–1 utilization. Aynı isimli pencerelerle birleştirilir.
- `provider/Layers/codexUsageLimits.ts`: `account/rateLimits/read` ve `updated`;
  ana `codex` allowance seçilir, model kapsamlı limit onun yerini almaz.
- Ekran görüntüsündeki API estimate gerçek abonelik faturası veya görev bazlı kota
  değildir. Daha sık okuma ve ondalıkları koruma, provider'ın vermediği kesinliği
  üretmez. Diğer oturumlar, sıfırlamalar ve kaba ölçüm ayrıca değerlendirilir.

## Doğrudan limit okuma

`cimrihook quota --agent claude|codex` mevcut CLI oturumunun kimlik doğrulamasını
kullanır. Python standart kütüphanesiyle subprocess/control IPC uygulanır;
credential dosyaları okunmaz veya kopyalanmaz. Claude initialization + `get_usage`,
Codex initialize + initialized + `account/rateLimits/read` kullanılır. Hiçbir
user prompt veya model turn gönderilmez. Hooks ve MCP discovery Claude probe'unda
kapalıdır. Deadline ortak ve sınırlıdır; süreç her durumda kapatılır.

Çıktı türü: provider, observed_at, available ve normalleştirilmiş pencere kayıtları.
Her pencerede id, used_percent, reset_at ve varsa duration bulunur. Ondalıklar
korunur; eksik bilgi sıfırla doldurulmaz. Unsupported ayrı raporlanır; protokol,
timeout, auth ve bozuk response hataları açıkça hata verir. Snapshotlar session
USD ledger'ına eklenmez; olmayan maliyet atfetmek veya hesapları karıştırmak yoktur.

## Deneysel görev kapatma

Varsayılan kapalı ayrı mod modülü; yalnız pilotun env ayarıyla etkinleşir.
Warm-up sonrasında harness ayrı bir arm dosyası yazar. Yalnız belirlenmiş fix
turn'ünün başında engine mesajlarının kanonik başlangıcı yakalanır ve dondurulur;
sonraki prompt'lar bu anchor'ı değiştirmez. Görev sonunda dış harness testleri çalıştırır, test dosyalarının
değişmediğini ve workspace durumunu doğrular. Başarısız görev için kapatma isteği
yazılmaz. Doğrulanan istekte session id, anchor'ın kanonik metni, tamamlanmış
mesajların kanonik metni, test kanıtı ve doğrulanan fixture dosyalarının tam
içerikleri bulunur. Mod kapanıştan önce fixture dosyalarını tekrar okur; farklı
state reddedilir. Request bir kez tüketilir; ikinci resume'da uygulanmaz. Ardından
anchor yeniden yakalanmaz. Probe home host'un oluşturduğu private dizindir.

Sonraki idle main prompt'tan önce mod mevcut `session.compact` arayüzünü kullanır.
Kapatma handler'ı başlangıçtaki mesajların hâlâ aynı prefix olduğunu kontrol eder.
Değişmiş prefix, eksik/bozuk kanıt veya task ortasında native compaction varsa
deney kapatma girişimi hata olarak kaydedilir; `{ skip: reason }` veto sonucu
döner, downstream çağrılmaz ve başarı sayılmaz. Archive write hatası da veto olur.
Closure dispatch'ine bağlı scoped catch hook exception/timeout'ta da veto döner.
Kontrol sağlanırsa başlangıç mesajları engine handle'larıyla korunur, task içindeki
bütün özgün user prompt/düzeltmeleri ve son assistant sonuç metni tutulur, tool
sonuçları/ara assistant adımları aktif geçmişten çıkarılır. Dış doğrulama kanıtı
eklenir. Önce projected mesaj arşivi yazılır. Bu thinking/attachment açısından tam
transcript yedeği değildir; host ayrıca özgün CLI JSONL transcript'ini kapatma
öncesinde kopyalar. Handle yalnız mevcut compact event'inden alınır.

Doğrulama başarısı yalnız belirli fixture'ın kabul ölçütünü gösterir. Kısa assistant
sonucu gelecekte gereken kararları taşımazsa pilot kalite kontrolü başarısızdır.
İlk sürümde modelle özet üretme, rastgele görev sınırı sınıflandırma, düşük effort,
provider routing veya kullanıcı ayarlarını değiştirme yoktur.

## Pilot komutu ve veri

`cimrihook closure-probe` kendine ait geçici workspace ve private meter home kurar.
İlk turn sıcak bir ortak başlangıç oluşturur; sonraki turn ayrı büyük observation
okur ve küçük fixture hatasını düzeltir. Host testleri değişmeyen test dosyalarıyla
doğrular. Sonuç kaydı ve kapatma isteği hazırlanır. Sonraki CLI resume görev
kapatmayı tetikler. Bir ek resume küçülen geçmişin kalıcı olduğunu kontrol eder.

Sonuç JSON'u CLI version/model/effort, fixture doğrulaması, arşiv ve kapatma
metrikleri, turn bazlı provider token usage/cost, varsa öncesi/sonrası quota
snapshotları içerir. Cache başarısı ve resume başarısı ayrı boolean'lar olur.
Cache gate yalnız kapatma sonrası ilk main model isteğini kullanır. Warm-up ilk
isteğinin toplam input'u static/tool başlangıç için taban ölçümdür. Fix ilk
isteğinde cache read bunun en az 8192 token üzerinde olmalı (conversation anchor
gerçekten cache'li). Kapatma sonrası ilk isteğin cache read'i fix ilk isteğinin en
az %95'i olmalı. Son warm pre-close isteği ile post-close isteği arası en fazla
300 saniye olmalı ve model, effort, tools/settings aynı kalmalı. Eksik ölçüm veya
yetersiz anchor negatif gate ve açıklama üretir. Turn toplamı veya cumulative
modelUsage gate için kullanılmaz. Persistence gate, ilk kapatma sonrası ve ikinci
resume başlangıcında disposable observation marker'ının bulunmamasıdır.
Engine'ın mod handle'ı veya resume davranışı beklentiyi sağlamazsa sonuç negatiftir;
tasarruf iddiası yazılmaz. Ham token sayıları provider response'undan gelir;
karakter boyutları token diye sunulmaz. Pilot quota deltasını hesap geneli gözlem
olarak raporlar; background kullanımın görev atfını belirsizleştirdiğini belirtir.

## Sağlamlık düzeltmesi

Idle/cold compaction'da `compacted` yalnız başarılı outcome'dan sonra kalıcı olur.
Devam eden compaction için ayrı flag yarışları engeller; skip/error bunu temizler.
Hatalar sessizce yutulmaz. Sonraki timer tick gerekli olduğunda yeniden deneyebilir.

## Doğrulama ve kabul

- Mevcut Python ruff/mypy/pytest kapıları geçmeli.
- CLI kontrol istemcilerinin gerçek Claude ve Codex read-only çağrıları denenmeli.
- Yeni veri dönüşümleri ve hata durumları için minimum anlamlı testler.
- Mod engine'ın kendi validate/test arayüzüyle kontrol edilmeli.
- Gerçek closure pilotu çalıştırılıp cache ve iki resume sonucu raporlanmalı.
- Sonuç negatifse deney araçları opt-in kalmalı, üretim önerisi yapılmamalı.
- Geniş kalite/A-B gate ilk pilotun devam işidir: mevcut governor'a karşı bağlı
  görevler, önceki kararları hatırlama ve uzun oturumlar olmadan aynı kalite kanıtı yoktur.
