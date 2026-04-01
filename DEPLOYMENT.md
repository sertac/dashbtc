# 🚀 BTC/USDT Signal Bot - Deployment Guide

## 📦 Production Hazırlık

### 1. Bağımlılıkları Yükle
```bash
cd /Users/sertac/dashbtc
source .venv/bin/activate
pip install -r requirements.txt
pip install gunicorn  # Production WSGI server
```

### 2. Test Et
```bash
# Local test
python wsgi.py

# Production test (gunicorn)
gunicorn wsgi:application --bind 0.0.0.0:5007 --workers 2
```

---

## 🌐 Deploy Seçenekleri

### **Seçenek 1: AlwaysData** (Mevcut .alwaysdata.ini)

1. **AlwaysData'ya git:** https://www.alwaysdata.com/
2. **Site oluştur:** Web > Websites > Create a website
3. **Git ile deploy:**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add alwaysdata https://user@dashbtc.alwaysdata.net/git/dashbtc.git
   git push alwaysdata main
   ```
4. **WSGI ayarla:** Web > WSGI > Add a WSGI application
   - Path: `/`
   - Application: `wsgi:application`

---

### **Seçenek 2: Railway** (En Kolay ⭐)

1. **Railway'a git:** https://railway.app/
2. **GitHub repo bağla** veya **Deploy from GitHub**
3. **Otomatik algılar:** Procfile'ı okur
4. **Environment variables ekle:**
   - `FLASK_ENV=production`
   - `SECRET_KEY=random-string`
5. **Deploy!** Otomatik başlar

**URL:** `https://dashbtc-production.up.railway.app`

---

### **Seçenek 3: Render** (Free Tier ⭐)

1. **Render'a git:** https://render.com/
2. **New Web Service** > GitHub repo seç
3. **Build Command:** `pip install -r requirements.txt`
4. **Start Command:** `gunicorn wsgi:application`
5. **Environment Variables:**
   - `FLASK_ENV=production`
   - `PYTHON_VERSION=3.11.0`

**URL:** `https://dashbtc.onrender.com`

---

### **Seçenek 4: PythonAnywhere** (Flask-Friendly)

1. **PythonAnywhere'a git:** https://www.pythonanywhere.com/
2. **Web > Add a new web app**
3. **Flask seç** > Python 3.10
4. **WSGI configuration file düzenle:**
   ```python
   import sys
   path = '/home/yourusername/dashbtc'
   if path not in sys.path:
       sys.path.append(path)
   from wsgi import application
   ```
5. **Files > Upload:** Tüm dosyaları yükle
6. **Bash console:**
   ```bash
   cd ~/dashbtc
   pip install --user -r requirements.txt
   ```

**URL:** `https://yourusername.pythonanywhere.com`

---

### **Seçenek 5: VPS (DigitalOcean / AWS)**

1. **Server kur (Ubuntu 22.04):**
   ```bash
   # SSH ile bağlan
   ssh root@your-server-ip
   
   # Update
   apt update && apt upgrade -y
   
   # Python install
   apt install python3-pip python3-venv nginx -y
   
   # App kur
   mkdir -p /var/www/dashbtc
   cd /var/www/dashbtc
   git clone https://github.com/yourusername/dashbtc.git .
   
   # Virtualenv
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt gunicorn
   ```

2. **Systemd service oluştur:**
   ```bash
   nano /etc/systemd/system/dashbtc.service
   ```
   
   ```ini
   [Unit]
   Description=BTC Signal Bot
   After=network.target
   
   [Service]
   User=www-data
   Group=www-data
   WorkingDirectory=/var/www/dashbtc
   ExecStart=/var/www/dashbtc/venv/bin/gunicorn wsgi:application \
       --bind unix:/var/www/dashbtc/dashbtc.sock \
       --workers 2 \
       --threads 4
   
   [Install]
   WantedBy=multi-user.target
   ```

3. **Nginx config:**
   ```bash
   nano /etc/nginx/sites-available/dashbtc
   ```
   
   ```nginx
   server {
       listen 80;
       server_name your-domain.com;
       
       location / {
           include proxy_params;
           proxy_pass http://unix:/var/www/dashbtc/dashbtc.sock;
       }
   }
   ```

4. **Başlat:**
   ```bash
   systemctl enable dashbtc
   systemctl start dashbtc
   systemctl restart nginx
   ```

---

## 🔒 Güvenlik Checklist

- [ ] `SECRET_KEY` değiştir (`.env` dosyasında)
- [ ] `FLASK_DEBUG=False` (production)
- [ ] HTTPS kullan (Let's Encrypt)
- [ ] Firewall ayarla (UFW)
- [ ] Rate limiting ekle (Flask-Limiter)
- [ ] Database backup scripti

---

## 📊 Monitoring

### Health Check Endpoint Ekle
```python
@app.route("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
```

### Log Monitoring
```bash
# Real-time logs
tail -f /var/log/dashbtc/error.log

# Gunicorn logs
journalctl -u dashbtc -f
```

---

## 🆘 Sorun Giderme

### "Module not found"
```bash
pip install -r requirements.txt
```

### "Port already in use"
```bash
lsof -ti:5007 | xargs kill -9
```

### "Database locked"
```bash
rm signals.db-wal signals.db-shm
```

### "Permission denied"
```bash
chmod +x wsgi.py
chown -R www-data:www-data /var/www/dashbtc
```

---

## ✅ Deploy Sonrası Test

1. **Ana sayfa:** `https://your-domain.com/`
2. **Stream endpoint:** `https://your-domain.com/stream`
3. **Health check:** `https://your-domain.com/health`
4. **WebSocket:** Browser'da aç, veri akıyor mu?

---

## 📞 Yardım

Sorun yaşarsan:
1. Logları kontrol et: `tail -100 /var/log/dashbtc/error.log`
2. Gunicorn status: `systemctl status dashbtc`
3. Nginx status: `systemctl status nginx`

Good luck! 🚀
