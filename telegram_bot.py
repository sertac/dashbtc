#!/usr/bin/env python3
"""
Telegram Bot - Long Polling Mode
Run: python3 telegram_bot.py
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("❌ Token veya Chat ID eksik!")
    sys.exit(1)

print("🤖 Telegram Bot Başlatılıyor...")
print(f"✅ Token: {TOKEN[:30]}...")
print(f"✅ Chat ID: {CHAT_ID}")
print()

# Son mesaj ID'sini takip et (aynı mesajı iki kere işleme)
last_update_id = 0

# Dashboard'dan veri almak için
DASHBOARD_URL = "http://localhost:5007/stream"

def get_dashboard_data():
    """Dashboard'dan anlık veri al."""
    try:
        # İlk mesajı al (stream formatında)
        import re
        resp = requests.get(DASHBOARD_URL, timeout=10, stream=True)
        if resp.status_code == 200:
            # İlk "data: {...}" satırını al
            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data:'):
                        match = re.search(r'data:\s*({.+})', line)
                        if match:
                            import json
                            return json.loads(match.group(1))
                        break
    except Exception as e:
        print(f"[HATA] Dashboard veri alınamadı: {e}")
    return None

# Komut işleme
def telegram_handle_command(command, data=None):
    """Telegram komutlarını işle."""
    command = command.lower().strip()
    
    if command == '/start' or command == '/help':
        message = """
🤖 <b>BTC Signal Bot - Yardım</b>

📋 Komutlar:
/status - Anlık piyasa durumu
/signals - Bekleyen sinyaller
/stats - Win rate istatistikleri
/ping - Bot durumu

#Help
"""
        telegram_send_message(message.strip())
        return True
    
    elif command == '/ping':
        message = """
🟢 <b>BOT ÇALIŞIYOR</b>

✅ Sistem durumu: İyi
📊 Symbol: ETH/USDT
⏰ Son güncelleme: {ts}

#Ping
""".format(ts=time.strftime("%H:%M:%S")).strip()
        telegram_send_message(message)
        return True
    
    elif command == '/status':
        # Dashboard'dan veri al
        if not data:
            data = get_dashboard_data()
        
        if not data:
            telegram_send_message("⚠️ Dashboard'dan veri alınamadı!")
            return True
        
        # Verileri çıkar
        price = data.get("price", 0)
        rsi = data.get("rsi", 0)
        ema_fast = data.get("ema_fast", 0)
        ema_slow = data.get("ema_slow", 0)
        
        mkt = data.get("mkt", {})
        htf = data.get("htf", {})
        
        message = """
📊 <b>PİYASA DURUMU</b>

📈 <b>ETH/USDT</b>
💰 Fiyat: ${price}
📊 RSI: {rsi}
📉 EMA: {ema_fast}/{ema_slow}

🎯 <b>HTF Trend:</b> {htf_trend}
💧 Funding: {funding}
📊 OI: {oi}
📈 L/S: {ls}
🔥 Taker: {taker}

#Status
""".format(
            price=price,
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            htf_trend=htf.get("trend", "—"),
            funding=mkt.get("funding_str", "—"),
            oi=mkt.get("oi_trend", "—"),
            ls=mkt.get("ls_str", "—"),
            taker=mkt.get("taker_str", "—")
        ).strip()
        telegram_send_message(message)
        return True
    
    elif command == '/signals':
        # Dashboard'dan veri al
        if not data:
            data = get_dashboard_data()
        
        if not data:
            telegram_send_message("⚠️ Dashboard'dan veri alınamadı!")
            return True
        
        pending = data.get("pending", [])
        if not pending:
            telegram_send_message("⚪ <b>Bekleyen sinyal yok</b>")
            return True
        
        message = "📋 <b>BEKLEYEN SİNYALLER</b>\n\n"
        for sig in pending[:5]:  # Max 5 sinyal
            direction_emoji = "🟢" if sig.get("dir") == "LONG" else "🔴"
            message += f"""
{direction_emoji} <b>{sig.get("dir")}</b> @ ${sig.get("entry", 0)}
🎯 TP: ${sig.get("tp", 0)}
🛑 SL: ${sig.get("sl", 0)}
⭐ {sig.get("score", 0)}/4 ({sig.get("conf_total", 0)}/100)

"""
        message += "#Signals"
        telegram_send_message(message.strip())
        return True
    
    elif command == '/stats':
        # Dashboard'dan veri al
        if not data:
            data = get_dashboard_data()
        
        if not data:
            telegram_send_message("⚠️ Dashboard'dan veri alınamadı!")
            return True
        
        stats = data.get("stats", {})
        total = stats.get("total", 0)
        wins = stats.get("wins", 0)
        win_rate = stats.get("win_rate", 0)
        
        if win_rate >= 60:
            emoji = "🔥"
        elif win_rate >= 45:
            emoji = "⚪"
        else:
            emoji = "❄️"
        
        message = """
{emoji} <b>İSTATİSTİKLER</b> {emoji}

📊 <b>ETH/USDT</b>
📈 Toplam: {total} sinyal
{color} <b>Win: {wins} | Loss: {losses}</b>
🎯 <b>Win Rate: {win_rate}%</b>
💰 Net P/L: {net_pct:+.2f}% (${net_usd:+.2f})

#Stats
""".format(
            emoji=emoji,
            total=total,
            wins=wins,
            losses=stats.get("losses", 0),
            win_rate=win_rate,
            net_pct=stats.get("net_pnl_pct", 0),
            net_usd=stats.get("net_pnl_usd", 0),
            color="✅" if win_rate >= 50 else "❌"
        ).strip()
        telegram_send_message(message)
        return True
    
    return False

def telegram_send_message(message):
    """Telegram'a mesaj gönder."""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        r = requests.post(url, json=data, timeout=5)
        result = r.json()
        if result.get("ok"):
            print(f"[TG] Mesaj gönderildi: {message[:50]}...")
            return True
        else:
            print(f"[TG HATA] {result}")
            return False
    except Exception as e:
        print(f"[TG HATA] {e}")
        return False

print("✅ Bot çalışıyor! Komutları bekle...")
print("   Durdurmak için: Ctrl+C")
print()

try:
    while True:
        # Telegram'dan update'leri al
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 30}
        
        try:
            response = requests.get(url, params=params, timeout=35)
            response.raise_for_status()
            result = response.json()
            
            if result.get("ok"):
                updates = result.get("result", [])
                
                for update in updates:
                    last_update_id = update.get("update_id", 0)
                    message = update.get("message", {})
                    
                    # Sadece bizim chat'imizden gelenleri işle
                    if str(message.get("chat", {}).get("id", "")) != str(CHAT_ID):
                        continue
                    
                    text = message.get("text", "")
                    
                    # Komutları işle
                    if text.startswith("/"):
                        print(f"📩 Komut alındı: {text}")
                        # Dashboard verisini al ve komutu işle
                        data = get_dashboard_data()
                        telegram_handle_command(text, data)
                        
        except requests.exceptions.Timeout:
            pass  # Normal, uzun polling timeout
        except Exception as e:
            print(f"❌ Hata: {e}")
            time.sleep(5)
            
        time.sleep(1)
        
except KeyboardInterrupt:
    print("\n👋 Bot durduruldu.")
