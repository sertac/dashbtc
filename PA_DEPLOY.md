# 🐍 PythonAnywhere Deploy Rehberi

## 📋 Adım Adım Kurulum

### **1. Hesap Oluştur**
1. https://www.pythonanywhere.com/ → **Sign Up**
2. **Free Account** seç
3. **Username:** `sertac` (veya müsait bir isim)
4. Email doğrula

**Free Limitler:**
- ✅ 512MB disk alanı
- ✅ 1 web app
- ✅ Python 3.10
- ⚠️ Dış API whitelist (Binance için gerekli)

---

### **2. Git ile Deploy (Önerilen)**

**PythonAnywhere Bash Console'da:**
```bash
# Home dizinine git
cd /home/sertac

# Git repo'yu clone et
git clone https://github.com/sertac/dashbtc.git
cd dashbtc

# Virtualenv oluştur
python3 -m venv venv
source venv/bin/activate

# Bağımlılıkları yükle
pip install -r requirements.txt

# Gerekirse signals.db'yi yükle
# (veya ilk çalıştırmada oluşacak)
```

---

### **3. Manuel Upload (Alternatif)**

**Files** sekmesi → **Upload** butonu:

1. `btc35.py` → Upload
2. `wsgi_pa.py` → Upload
3. `requirements.txt` → Upload
4. `signals.db` → Upload (varsa)

**Sonra Bash Console:**
```bash
cd /home/sertac
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

### **4. WSGI Configuration**

**Web** sekmesi → **yourusername.pythonanywhere.com** → **WSGI configuration file**

**İçeriği sil, şunu yapıştır:**
```python
import sys
import os

# Project directory
project_home = '/home/sertac/dashbtc'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Activate virtualenv
activate_env = os.path.join(project_home, 'venv/bin/activate_this.py')
if os.path.exists(activate_env):
    with open(activate_env) as f:
        exec(f.read(), {'__file__': activate_env})

# Environment variables
os.environ['FLASK_ENV'] = 'production'
os.environ['SECRET_KEY'] = 'pythonanywhere_secret_2026'

# Import Flask app
from btc35 import app as application
```

**Save** → **Reload** butonuna bas!

---

### **5. Background Task (Sinyal Loop)**

**Tasks** sekmesi → **Add a scheduled task**

**Schedule:** `00:00` (Her gün gece yarısı)

**Command:**
```bash
cd /home/sertac/dashbtc && \
source venv/bin/activate && \
python3 -c "
import time, threading, sys
sys.path.insert(0, '/home/sertac/dashbtc')
from btc35 import background_loop
print('[TASK] Starting background loop...')
thread = threading.Thread(target=background_loop, daemon=True)
thread.start()
time.sleep(86400)  # 24 saat çalış
print('[TASK] Task completed')
"
```

**Run now** → Başlat!

---

### **6. Binance API Whitelist Başvurusu**

**ÖNEMLİ:** Free account'ta dış API'ler engelli.

**Help** → **Contact Us** → Form doldur:

```
Subject: API Whitelist Request for Binance API

Message:
---------
Hi PythonAnywhere Team,

I'm deploying a crypto signal bot that needs to access Binance API 
for real-time market data (prices, orderbook, funding rates).

Required domains:
- fapi.binance.com (port 443)
- api.binance.com (port 443)
- www.reutersagency.com (port 443)
- feeds.bbci.co.uk (port 443)

This is a free account project for educational purposes.
URL: https://sertac.pythonanywhere.com

Thank you for your help!

Best regards,
Sertac
```

**Onay süresi:** 24-48 saat

---

### **7. Test**

**Canlı URL:** `https://sertac.pythonanywhere.com`

**Kontroller:**
- [ ] Ana sayfa yükleniyor ✅
- [ ] Dashboard verileri görünüyor ✅
- [ ] Stream bağlantısı çalışıyor ✅
- [ ] Background loop çalışıyor (log'larda) ✅

---

## 🔧 Sorun Giderme

### **"No module named 'btc35'"**
```bash
# Bash Console'da:
cd /home/sertac/dashbtc
source venv/bin/activate
pip install -r requirements.txt
```

### **"Database locked"**
```bash
# Bash Console:
cd /home/sertac/dashbtc
rm -f signals.db-wal signals.db-shm
```

### **"Background loop çalışmıyor"**
```
Tasks > Last run > View log
```

### **"Binance API hatası"**
```
Whitelist onayı bekleniyor (24-48 saat)
Geçici çözüm: Localhost'ta test et
```

---

## 📊 Loglar

**Web App Logs:**
```
Web > yourusername.pythonanywhere.com > Error log
Web > yourusername.pythonanywhere.com > Server log
```

**Task Logs:**
```
Tasks > Last run > View log
```

---

## 🆘 Yardım

**PythonAnywhere Forum:**
https://www.pythonanywhere.com/forums/

**Sorun yaşarsan:**
1. Error log'u kontrol et
2. Task log'u kontrol et
3. Forum'da ara
4. Contact Us ile yaz

---

## ✅ Deploy Checklist

- [ ] PythonAnywhere hesabı oluşturuldu
- [ ] Git repo clone edildi veya dosyalar upload edildi
- [ ] Virtualenv oluşturuldu
- [ ] Requirements install edildi
- [ ] WSGI config düzenlendi
- [ ] Background task eklendi
- [ ] Binance whitelist başvurusu yapıldı
- [ ] Reload yapıldı
- [ ] Test edildi

---

**Good luck! 🚀**
