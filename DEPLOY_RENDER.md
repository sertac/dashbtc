# 🚀 Render'a Deploy — BTC Signal Bot

## Ön Hazırlık

### 1. GitHub'a Push
```bash
cd /Users/sertac/dashbtc
git add -A
git commit -m "Render deployment ready"
git push origin main
```

### 2. Neon PostgreSQL Ücretsiz Veritabanı (5 dakika)
1. [neon.tech](https://neon.tech) → Sign up (GitHub ile)
2. **Create Project** → İsim: `btc-signal-bot`
3. Connection string'i kopyala: `postgres://user:pass@ep-xxx.us-east-2.aws.neon.tech/botdb`

### 3. UptimeRobot Ücretsiz (Keepalive için)
1. [uptimerobot.com](https://uptimerobot.com) → Sign up
2. **Add Monitor** → Type: `HTTP(s)`
3. URL: `https://<render-app-name>.onrender.com/keepalive`
4. Interval: **2 minutes**
5. Friendly Name: `BTC Signal Bot`

---

## Render Deployment

### 1. Render'a Bağlan
1. [render.com](https://render.com) → Sign up (GitHub ile)
2. **New +** → **Blueprint**
3. GitHub repo'nu seç: `sertac/dashbtc`
4. `render.yaml` otomatik okunur

### 2. Environment Variables
Render dashboard'da şu env varları ekle:

| Key | Value |
|-----|-------|
| `SYMBOL` | `ETH/USDT` |
| `DATABASE_URL` | `postgres://...` (Neon connection string) |
| `BINANCE_API_KEY` | `...` (opsiyonel) |
| `BINANCE_SECRET_KEY` | `...` (opsiyonel) |
| `TELEGRAM_BOT_TOKEN` | `...` (opsiyonel) |
| `TELEGRAM_CHAT_ID` | `...` (opsiyonel) |

### 3. Deploy
- **Apply** → Build başlar (~2 dakika)
- **Logs** sekmesinden `[WSGI] Background loop thread started` mesajını kontrol et
- URL: `https://btc-signal-bot.onrender.com`

### 4. UptimeRobot'u Aktif Et
- Render deploy bittikten sonra UptimeRobot monitor'ünü aktif et
- Her 2 dakikada bir `/keepalive` endpoint'ini pingler
- Bu sayede Render sleep'e geçmez

---

## Doğrulama

### Health Check
```bash
curl https://btc-signal-bot.onrender.com/health
# {"status": "ok", "symbol": "ETH/USDT", "timestamp": "19:30:00", "pending_signals": 0}
```

### Log Kontrolü
Render dashboard → **Logs** sekmesi:
```
[DB] PostgreSQL mode — postgres://user:pass@ep-xxx...
[WSGI] Database initialized and signals loaded
[WSGI] Background loop thread started
[HTF] NEUTRAL bull=1 bear=2
[MKT] FR=0.0028% OI=nötr L/S=1.587 Taker=1.05
```

### Dashboard
```
https://btc-signal-bot.onrender.com
```

---

## Sorun Giderme

### Sleep'e geçiyor
- UptimeRobot monitor'ünün çalıştığından emin ol
- `/keepalive` endpoint'inin 200 döndüğünü kontrol et

### DB bağlantı hatası
- `DATABASE_URL` doğru mu kontrol et
- Neon dashboard'dan IP whitelist'e `0.0.0.0/0` ekle

### Background loop çalışmıyor
- Log'da `[WSGI] Background loop thread started` var mı?
- Yoksa wsgi.py'de hata var, logları kontrol et

### SQLite modunda çalışıyor (DATABASE_URL yoksa)
- Render ephemeral filesystem → her deploy'da DB sıfırlanır
- **Mutlaka DATABASE_URL set et** (Neon ücretsiz)
