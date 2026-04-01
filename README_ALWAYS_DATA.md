# AlwaysData Deployment Guide for BTC33 Signal Bot

## 1. Upload Files
Upload these files to your AlwaysData account via FTP or File Manager:
- `btc33_alwaysdata.py` (rename to `btc33.py` on server)
- `requirements.txt`

## 2. Install Dependencies
Via SSH or web terminal:
```bash
pip install -r requirements.txt --user
```

## 3. Configure Python App
In AlwaysData Panel:
- Go to **Applications** → **Add Application**
- Type: **Python**
- Path: `/` (or your subdirectory)
- Version: **Python 3.11**

## 4. Set WSGI Entry Point
Create or edit `.alwaysdata.ini` in your root directory:
```ini
[python]
path = /
module = btc33
callable = app
```

## 5. Access Dashboard
Your bot will be available at:
`https://your-username.alwaysdata.net/`

## Notes
- SQLite database (`signals.db`) will be created automatically
- WAL mode is enabled for better concurrent access
- Background thread runs continuously for signal generation
- Access the dashboard from any browser
