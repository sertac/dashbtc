#!/usr/bin/env python3
"""
Telegram Bot Test Script
Run: python3 test_telegram.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

# Değerleri al
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

print("=" * 50)
print("🤖 Telegram Bot Test")
print("=" * 50)

# Kontrol
if not TOKEN:
    print("❌ HATA: TELEGRAM_BOT_TOKEN bulunamadı!")
    print("   .env dosyasına ekleyin:")
    print("   TELEGRAM_BOT_TOKEN=123456789:ABCdef...")
    sys.exit(1)

if not CHAT_ID:
    print("❌ HATA: TELEGRAM_CHAT_ID bulunamadı!")
    print("   .env dosyasına ekleyin:")
    print("   TELEGRAM_CHAT_ID=123456789")
    print("\n💡 Chat ID bulmak için: @userinfobot'a /start yaz")
    sys.exit(1)

print(f"✅ Token: {TOKEN[:30]}...")
print(f"✅ Chat ID: {CHAT_ID}")
print()

# Test mesajı
print("📤 Test mesajı gönderiliyor...")
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
data = {
    "chat_id": CHAT_ID,
    "text": """
🟢 <b>TEST BAŞARILI!</b>

✅ Bot çalışıyor
✅ Token doğru
✅ Chat ID doğru

📊 BTC Signal Bot
"""
}

try:
    response = requests.post(url, json=data, timeout=5)
    result = response.json()
    
    if result.get("ok"):
        print("✅ BAŞARILI!")
        print()
        print("📱 Telegram'ı kontrol et!")
        print("   Bot'tan mesaj gelmiş olmalı.")
        print()
        print("💡 Komutları dene:")
        print("   /ping - Bot durumu")
        print("   /status - Piyasa durumu")
        print("   /signals - Bekleyen sinyaller")
        print("   /stats - Win rate istatistikleri")
    else:
        error = result.get("description", "Bilinmeyen hata")
        print(f"❌ Telegram Hatası: {error}")
        print()
        if "Unauthorized" in error:
            print("💡 Token yanlış! BotFather'dan yeni token al.")
        elif "chat not found" in error:
            print("💡 Chat ID yanlış! @userinfobot'tan doğru ID'yi al.")
        elif "bot was blocked" in error:
            print("💡 Bot'u engellemiş olabilirsin. Bot'u açıp /start yaz.")
        
except requests.exceptions.Timeout:
    print("❌ Timeout! Telegram API'ye bağlanılamadı.")
    print("   İnternet bağlantını kontrol et.")
except Exception as e:
    print(f"❌ Beklenmeyen hata: {e}")

print()
print("=" * 50)
