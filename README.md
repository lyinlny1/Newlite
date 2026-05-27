# Newlite Research

Research-only MVP untuk screening token Solana baru dari PumpPortal, enrichment dari DEX Screener, social research gratis tanpa X API, dan reasoning via Newlite internal agent atau Hermes Agent resmi NousResearch.

Bot ini tidak melakukan auto-buy, auto-sell, atau menyimpan private key.

## Fitur

- Scan token baru dari PumpPortal `subscribeNewToken`.
- Cek pair/profile token di DEX Screener.
- Ambil website/social link dari DEX Screener.
- Research gratis via DuckDuckGo HTML search dan website check.
- Rule-based score `0-100`.
- Opportunity score `0-100` untuk mencari kandidat yang berpotensi naik tinggi.
- SQLite memory untuk menyimpan token yang pernah discan.
- Follow-up otomatis untuk token yang pernah ditemukan, termasuk perubahan price/market cap.
- Token yang lolos alert dimonitor tiap 30 menit, lalu dikesampingkan kalau setelah 2 jam tidak ada kenaikan price/market cap.
- Daily research log 24 jam: jumlah scan, alert, follow-up, audit, token yang dimonitor, MC awal, dan perubahan performa.
- Migration monitor via PumpPortal `subscribeMigration` untuk token watchlist yang bonding curve selesai/migrasi.
- Telegram alert memakai HTML formatting, bold text, dan emoji agar mudah dibaca di mobile.
- Filter market cap: default hanya alert token MC `$25K-$200K`.
- Pump.fun/PumpSwap-focused: token baru diambil dari PumpPortal dan alert DEX difilter ke PumpSwap/Pump.fun.
- Risk/narrative detector: narasi token, bot-like activity, dev-sold signal, dan OKX Web3 enrichment untuk sniper/bundle/smart wallet jika API key tersedia.
- Newlite internal reasoning agent dengan provider fallback:
  - `openrouter`
  - `ollama_cloud`
  - `ollama_local`
  - `none`
- Telegram command:
  - `/status`
  - `/token <mint> [symbol] [name]`
  - `/deep <mint> [symbol] [name]`
  - `/why <mint>`
  - `/learn`
  - `/signals`
  - `/why_score <mint>`
  - `/top_peak`
  - `/narratives`
  - `/risk_report`
  - `/missed`
  - `/overrange`
  - `/copycats`
  - `/scan [limit] [timeout_seconds]`
  - `/scan_limit <jumlah>` optional runtime override
  - `/reasoning_limit <jumlah>`
  - `/monitor_start [interval_minutes]`
  - `/monitor_interval <minutes>`
  - `/followup_interval <minutes>`
  - `/daily_report`
  - `/monitor_stop`
  - `/monitor_status`
  - `/opportunities`

## Setup Dari Nol

### 1. Masuk ke folder project

```powershell
cd "D:\AI\Newlite Research"
```

### 2. Buat virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Jika PowerShell menolak aktivasi script:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependency

```powershell
pip install -r requirements.txt
```

### 4. Buat file `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Untuk mode gratis paling awal, isi minimal seperti ini:

```text
LLM_PROVIDER_ORDER=openrouter,ollama_cloud,ollama_local,none
OLLAMA_LOCAL_BASE_URL=http://localhost:11434/v1
OLLAMA_LOCAL_MODEL=llama3.1:8b
ENABLE_FREE_WEB_RESEARCH=true
MIN_ALERT_SCORE=70
MIN_MARKET_CAP_USD=25000
MAX_MARKET_CAP_USD=200000
OKX_WALLET_RISK_ENABLED=false
OKX_MONTHLY_REQUEST_LIMIT=999990
WALLET_RISK_ENABLED=false
SMART_WALLET_ENABLED=false
SMART_WALLET_ON_MIGRATION_ENABLED=true
SNIPER_DETECTION_ENABLED=false
BUNDLE_DETECTION_ENABLED=false
NEWLITE_AGENT_ENABLED=true
NEWLITE_AGENT_MIN_OPPORTUNITY_SCORE=45
MAX_REASONING_CALLS_PER_SCAN=5
DEV_SOLD_CHECK_ENABLED=false
DEV_SOLD_CHECK_SECONDS=6
HERMES_OFFICIAL_ENABLED=false
HERMES_CLI_COMMAND=hermes
HERMES_CLI_PROVIDER=
HERMES_CLI_MODEL=
HERMES_CLI_TIMEOUT_SECONDS=120
AUTO_MONITOR_ENABLED=false
MONITOR_INTERVAL_MINUTES=10
FOLLOWUP_INTERVAL_MINUTES=30
SIDELINE_AFTER_HOURS=2
DAILY_CHECK_INTERVAL_HOURS=24
DAILY_REPORT_TIME=13:00
MIGRATED_DISCOVERY_ENABLED=true
MIGRATED_DISCOVERY_LIMIT=20
```

### 5. Jalankan Ollama local

Install Ollama, lalu pull model:

```powershell
ollama pull llama3.1:8b
ollama serve
```

Kalau kamu memakai model Hermes di Ollama, ganti:

```text
OLLAMA_LOCAL_MODEL=nous-hermes2:10.7b
```

Lalu pull:

```powershell
ollama pull nous-hermes2:10.7b
```

### 6. Ollama Cloud sebagai fallback

Kalau ingin Ollama Cloud:

```text
LLM_PROVIDER_ORDER=openrouter,ollama_cloud,ollama_local,none
OLLAMA_CLOUD_API_KEY=isi_api_key_ollama_cloud
OLLAMA_CLOUD_BASE_URL=https://ollama.com/v1
OLLAMA_CLOUD_MODEL=nama_model_cloud_kamu
```

### 7. OpenRouter sebagai fallback premium

```text
OPENROUTER_API_KEY=isi_openrouter_key
OPENROUTER_MODEL=anthropic/claude-3.5-haiku
```

Urutan fallback contoh:

```text
LLM_PROVIDER_ORDER=openrouter,ollama_cloud,ollama_local,none
```

Jika semua provider gagal, bot tetap jalan memakai rule-based score.

Reasoning agent tidak dipanggil untuk semua token saat scan otomatis. Agar hemat token AI, reasoning hanya berjalan kalau kandidat sudah lolos prefilter:

- market cap >= `MIN_MARKET_CAP_USD`
- market cap <= `MAX_MARKET_CAP_USD`
- pair DEX harus Pump.fun/PumpSwap jika DEX ID tersedia
- opportunity score >= `NEWLITE_AGENT_MIN_OPPORTUNITY_SCORE`, atau ada X/social + volume/tx aktif
- maksimal `MAX_REASONING_CALLS_PER_SCAN` kandidat per siklus scan

OKX Web3 risk enrichment aktif kalau:

- `OKX_WALLET_RISK_ENABLED=true`
- `OKX_API_KEY`, `OKX_SECRET_KEY`, dan `OKX_PASSPHRASE` terisi
- opsi detail seperti `SMART_WALLET_ENABLED`, `SMART_WALLET_ON_MIGRATION_ENABLED`, `SNIPER_DETECTION_ENABLED`, dan `BUNDLE_DETECTION_ENABLED` diaktifkan

Bot memakai OKX Token Trades endpoint untuk tag `Developer`, `Smart Money`, `Sniper`, dan `Bundle`.

Dev-sold live check via PumpPortal `subscribeTokenTrade` tetap tersedia sebagai fallback, jadi hanya aktif kalau:

- `DEV_SOLD_CHECK_ENABLED=true`
- `PUMPPORTAL_API_KEY` terisi
- event token punya dev wallet/creator dari PumpPortal

Catatan penting: menurut PumpPortal, stream `subscribeTokenTrade`/`subscribeAccountTrade` membutuhkan API key dan bisa metered. Karena itu default-nya dimatikan agar bot tidak memakai biaya trade-stream diam-diam.

Command `/token <mint>` selalu mencoba reasoning agent karena itu analisis manual.

## Tes Token Manual

```powershell
python main.py token <TOKEN_MINT> --symbol HENRY --name Henry
```

Output berisi score, DEX link, liquidity/volume jika ada, social link, estimated free web interest, dan reasoning summary jika provider aktif.

Ada dua skor:

- `Opp`: opportunity score, dipakai untuk alert. Ini fokus ke potensi early/momentum move.
- `Trust`: trust/identity score, dipakai untuk menilai kelengkapan data dan kredibilitas awal.

## Agent Memory

Newlite memakai pola agent-memory ringan yang terinspirasi Meridian, tapi disesuaikan untuk research token, bukan DLMM execution:

- Decision log: setiap manual research, deep research, alert, follow-up, dan migration dicatat ke SQLite.
- `/why <mint>` menjelaskan keputusan terakhir untuk token itu.
- `/learn` merangkum outcome token yang pernah alert.
- `/signals` melihat sinyal mana yang historisnya lebih sering menang/dump.
- Prompt AI diberi aturan bahwa metadata token, website, social, dan search snippet adalah untrusted data.

Ini belum auto-trading dan tidak melakukan buy/sell. Memory dipakai untuk menjelaskan dan memperbaiki research.

## Scan Token Baru

```powershell
python main.py scan --limit 5 --timeout 60 --all
```

Tanpa `--all`, hanya token dengan score di atas `MIN_ALERT_SCORE` yang dicetak:

```powershell
python main.py scan --limit 20 --timeout 120
```

Catatan: PumpPortal `subscribeNewToken` dan `subscribeMigration` dipakai untuk MVP ini. Stream trade token/account bisa berbayar.

## Setup Telegram Bot

### 1. Buat bot di BotFather

Di Telegram:

```text
/newbot
```

Simpan token bot.

### 2. Cari Telegram user id

Cara cepat:

- Chat ke `@userinfobot`, atau
- Jalankan bot tanpa user id tidak disarankan.

Isi `.env`:

```text
TELEGRAM_BOT_TOKEN=token_dari_botfather
AUTHORIZED_TELEGRAM_USER_ID=angka_user_id_kamu
```

### 3. Jalankan bot

```powershell
python main.py bot
```

Di Telegram:

```text
/start
/status
/token <TOKEN_MINT> HENRY Henry
/scan 30 60
/reasoning_limit 5
/monitor_start 10
/followup_interval 30
/daily_report
/monitor_status
/monitor_stop
```

## Auto Screening

Ada dua cara.

Cara utama dari Telegram:

```text
/monitor_start 10
/followup_interval 30
```

Bot akan scan token baru sesuai interval yang kamu pilih selama proses `python main.py bot` tetap hidup. Default-nya bot membuka live scan window 10 menit penuh, lalu mengirim summary window tersebut dan lanjut ke window berikutnya. `SCAN_LIMIT` bukan durasi scan, tapi batas maksimal token yang dianalisis per window agar API/AI tidak boros. Follow-up token yang sudah lolos alert berjalan tiap 30 menit. Alert hanya dikirim kalau opportunity score token >= `MIN_ALERT_SCORE` dan market cap >= `MIN_MARKET_CAP_USD`.
Default watchlist market cap adalah `$25K-$200K`.
Setelah scan token baru, bot juga recheck token lama yang sudah tersimpan di memory untuk melihat perubahan price/market cap.
Kalau token yang sudah lolos tidak menunjukkan kenaikan price/market cap dalam 2 jam dari alert pertama, token itu akan dikesampingkan dari follow-up rutin.
Bot menjalankan audit token lama, termasuk yang sudah dikesampingkan, dan mengirim daily research log pada jam `DAILY_REPORT_TIME`. Default-nya `13:00` waktu server. Kamu juga bisa minta ringkasan manual dengan `/daily_report`.
Selama monitor hidup, bot juga menjalankan migration listener. Jika token yang pernah lolos alert kemudian bonding curve selesai/migrasi, bot akan recheck token itu dan mengirim `BONDING/MIGRATION UPDATE` sekali.
Bot juga menjalankan migrated discovery dari DEX Screener latest token profiles. Ini mencari token Solana terbaru yang sudah punya pair PumpSwap walaupun tidak pernah masuk scan memory, lalu mengirim `MIGRATED DISCOVERY` kalau lolos filter alert.
Follow-up tidak dikirim berulang hanya karena token masih bagus. Follow-up dikirim kalau ada perubahan price/market cap besar atau opportunity score naik signifikan.

Kalau ingin ubah interval saat monitor sudah berjalan:

```text
/monitor_interval 60
```

Kalau ingin ubah follow-up token lama:

```text
/followup_interval 30
```

Kalau ingin ubah analysis cap dan limit reasoning saat bot berjalan:

```text
/scan_limit 30
/reasoning_limit 5
```

Perubahan interval berlaku setelah siklus scan/sleep saat ini. Untuk apply langsung:

```text
/monitor_stop
/monitor_start 60
```

Cara otomatis saat bot dinyalakan:

```text
AUTO_MONITOR_ENABLED=true
MONITOR_INTERVAL_MINUTES=10
FOLLOWUP_INTERVAL_MINUTES=30
SIDELINE_AFTER_HOURS=2
DAILY_CHECK_INTERVAL_HOURS=24
DAILY_REPORT_TIME=13:00
MIGRATED_DISCOVERY_ENABLED=true
MIGRATED_DISCOVERY_LIMIT=20
SCAN_LIMIT=30
SCAN_TIMEOUT_SECONDS=60
MIN_ALERT_SCORE=70
MIN_MARKET_CAP_USD=25000
MAX_MARKET_CAP_USD=200000
OKX_WALLET_RISK_ENABLED=false
OKX_MONTHLY_REQUEST_LIMIT=999990
WALLET_RISK_ENABLED=false
SMART_WALLET_ENABLED=false
SMART_WALLET_ON_MIGRATION_ENABLED=true
SNIPER_DETECTION_ENABLED=false
BUNDLE_DETECTION_ENABLED=false
NEWLITE_AGENT_ENABLED=true
NEWLITE_AGENT_MIN_OPPORTUNITY_SCORE=45
MAX_REASONING_CALLS_PER_SCAN=5
DEV_SOLD_CHECK_ENABLED=false
DEV_SOLD_CHECK_SECONDS=6
HERMES_OFFICIAL_ENABLED=false
```

Lalu jalankan:

```powershell
python main.py bot
```

Dengan mode otomatis, bot akan mengirim alert ke `AUTHORIZED_TELEGRAM_USER_ID`.
Catatan: `SCAN_TIMEOUT_SECONDS` dipakai untuk command manual `/scan` dan CLI. Auto monitor memakai `MONITOR_INTERVAL_MINUTES` sebagai durasi scan window.
Catatan: `SCAN_LIMIT` mengatur maksimal token yang dianalisis dalam satu window auto monitor. Untuk mengubah permanen, edit `.env`; command `/scan_limit` hanya override sementara sampai bot restart.
Kalau ingin mematikan auto screening, ubah `AUTO_MONITOR_ENABLED=false`.

## Integrasi Hermes Agent Resmi NousResearch

Ada dua mode reasoning:

- `Newlite internal reasoning agent`: modul internal bot, bukan Hermes resmi.
- `Official Hermes`: NousResearch Hermes Agent yang diinstall terpisah dan dipanggil lewat `hermes chat -q`.

Saat `HERMES_OFFICIAL_ENABLED=true` dan command `hermes` ditemukan di PATH, bot memakai official Hermes. Jika tidak, bot fallback ke Newlite internal reasoning agent.

```text
Scanner -> DEX Enrichment -> Social Research -> Opportunity Score -> Official Hermes CLI -> Memory -> Follow-up/Migration -> Telegram Alert
```

Install resmi menurut docs NousResearch:

```powershell
pip install hermes-agent
hermes postinstall
hermes setup
```

Di Windows, docs resmi merekomendasikan WSL2 untuk install paling stabil. Alternatif official installer PowerShell dari repo:

```powershell
irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1 | iex
```

Setelah `hermes chat -q "hello"` berhasil dari terminal, aktifkan:

```text
HERMES_OFFICIAL_ENABLED=true
HERMES_CLI_COMMAND=hermes
HERMES_CLI_PROVIDER=openrouter
HERMES_CLI_MODEL=
```

## Batasan MVP

- Belum memakai X API, jadi virality hanya estimasi dari free web search.
- Belum memakai GMGN API atau OKX Web3 API. Itu bisa ditambah sebagai enrichment/risk/quote layer berikutnya.
- Tidak melakukan trading otomatis.
- Search gratis bisa tidak stabil karena bergantung pada HTML search result.

## Next Step Yang Masuk Akal

1. Tambah GMGN enrichment untuk sniper, insider, bundled wallet, top holder.
2. Tambah OKX Web3 quote/market data sebagai data pembanding DEX Screener.
3. Tambah dedup alert yang lebih pintar agar token sama tidak dikirim berkali-kali.
4. Tambah dashboard kecil kalau diperlukan.
