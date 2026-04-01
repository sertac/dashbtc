"""
BTC/USDT Sinyal Botu — Flask Dashboard (Tek Dosya)
"""

import json, time, threading, requests, re, html as html_lib, os, sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, Response, render_template_string, request as flask_request
import ccxt, pandas as pd, ta

SYMBOL         = "BTC/USDT"
AVAILABLE_SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT","DOGE/USDT"]
TIMEFRAME      = "5m"
CANDLE_LIMIT   = 100
HTF_TIMEFRAME  = "1h"
HTF_LIMIT      = 60
HTF_EMA_FAST   = 20
HTF_EMA_SLOW   = 50
HTF_RSI_OB     = 60
HTF_RSI_OS     = 40
HTF_REFRESH    = 60
BNFUT_BASE     = "https://fapi.binance.com"
MKT_REFRESH    = 30
FUND_STRONG    = 0.0008
FUND_WEAK      = -0.0008
LS_CROWD_LONG  = 1.4
LS_CROWD_SHORT = 0.7
TAKER_STRONG   = 1.25
OI_CHANGE_THR  = 0.005
OB_DEPTH       = 100
TOP_WALLS      = 6
BUCKET_PCT     = 0.0015
PROXIMITY_PCT  = 0.005
MIN_WALL_BTC   = 4.0
EMA_FAST       = 9
EMA_SLOW       = 21
RSI_PERIOD     = 14
RSI_OB         = 65
RSI_OS         = 35
VOL_MULTIPLIER = 1.8
TP_PCT         = 0.02
SL_PCT         = 0.01
COMMISSION     = 0.0015
MIN_SCORE      = 2
REFRESH_SEC    = 15
MAX_CANDLES_WAIT = 20
TWEET_REFRESH  = 60
NEWS_REFRESH   = 120
NEWS_MAX       = 40
TWEET_MAX      = 30

NEWS_FEEDS = [
    {"name": "Google News", "url": "", "dynamic": True},
    {"name": "CoinDesk",      "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt",       "url": "https://decrypt.co/feed"},
]

STOCKTWITS_MAP = {
    "BTC":"BTC.X", "ETH":"ETH.X", "SOL":"SOL.X",
    "BNB":"BNB.X", "XRP":"XRP.X", "DOGE":"DOGE.X",
}
REDDIT_SUBS = ["Bitcoin","CryptoCurrency","btc","ethereum","CryptoMarkets"]
REDDIT_HEADERS = {"User-Agent": "btc-dashboard/1.0", "Accept": "application/json"}

app      = Flask(__name__)
exchange = ccxt.binance({"options": {"defaultType": "future"}})

# ── SQLite Veritabanı ─────────────────────────────────────────
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db")

def db_connect():
    """Thread-safe SQLite bağlantısı."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    """Tabloları oluştur (yoksa)."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,
                entry        REAL    NOT NULL,
                tp           REAL    NOT NULL,
                sl           REAL    NOT NULL,
                score        INTEGER NOT NULL,
                net_tp_pct   REAL,
                net_sl_pct   REAL,
                wall_price   REAL,
                wall_vol     REAL,
                htf_trend    TEXT,
                status       TEXT    NOT NULL DEFAULT 'pending',
                outcome      TEXT,
                exit_price   REAL,
                net_pnl_pct  REAL,
                net_pnl_usd  REAL,
                duration_min INTEGER,
                open_ts      TEXT    NOT NULL,
                close_ts     TEXT,
                checks_json  TEXT
            )
        """)
        conn.commit()
        # Migrasyonlar — eski DB'lere yeni sütunlar ekle
        for col, typ in [('exit_price','REAL'), ('duration_min','INTEGER'), ('close_reason','TEXT')]:
            try:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
                conn.commit()
            except Exception:
                pass  # Sütun zaten var
    print(f"[DB] {DB_FILE}")

def db_insert_signal(sig):
    """Yeni pending sinyal ekle, rowid döndür."""
    with db_connect() as conn:
        cur = conn.execute("""
            INSERT INTO signals
              (symbol, direction, entry, tp, sl, score,
               net_tp_pct, net_sl_pct, wall_price, wall_vol,
               htf_trend, status, open_ts, checks_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
        """, (
            sig.get("symbol", SYMBOL),
            sig["dir"], sig["entry"], sig["tp"], sig["sl"], sig["score"],
            sig.get("net_tp_pct"), sig.get("net_sl_pct"),
            sig.get("wall_price"), sig.get("wall_vol"),
            sig.get("htf_trend"),
            sig.get("ts", datetime.now().strftime("%H:%M:%S")),
            json.dumps([c["label"] for c in sig.get("checks", [])]),
        ))
        conn.commit()
        return cur.lastrowid

def db_close_signal(rowid, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price=None, duration_min=None, close_reason=None):
    status = outcome.lower()
    with db_connect() as conn:
        conn.execute("""
            UPDATE signals SET status=?, outcome=?, net_pnl_pct=?, net_pnl_usd=?,
                               close_ts=?, exit_price=?, duration_min=?, close_reason=?
            WHERE id=?
        """, (status, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price, duration_min, close_reason, rowid))
        conn.commit()

def db_load_pending():
    """Bekleyen sinyalleri dict listesi olarak yükle."""
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE status='pending' ORDER BY id"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["dir"]     = d.pop("direction")
        d["checks"]  = [{"label": l, "status": "pass", "side": ""} for l in json.loads(d.pop("checks_json") or "[]")]
        d["_db_id"]  = d["id"]
        d["ts"]      = d.pop("open_ts")
        d["htf_blocked"] = False
        result.append(d)
    return result

def db_load_closed(symbol=None, limit=100):
    """Kapanmış sinyalleri yükle."""
    with db_connect() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM signals WHERE status!='pending' AND symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signals WHERE status!='pending' ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["dir"]     = d.pop("direction")
        d["checks"]  = []
        d["close_ts"]= d.get("close_ts") or d.get("open_ts","")
        result.append(d)
    return result

def db_win_stats(symbol=None):
    """Veritabanından win rate istatistiklerini hesapla."""
    with db_connect() as conn:
        q = "SELECT * FROM signals WHERE status != 'pending'"
        params = ()
        if symbol:
            q += " AND symbol = ?"
            params = (symbol,)
        rows = conn.execute(q, params).fetchall()
    closed = [dict(r) for r in rows]
    if not closed:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,
                "long_total":0,"long_wins":0,"long_rate":0,
                "short_total":0,"short_wins":0,"short_rate":0,
                "net_pnl_pct":0,"net_pnl_usd":0,
                "comm_pct":round(2*COMMISSION*100,2)}
    wins   = sum(1 for s in closed if s["outcome"]=="WIN")
    losses = len(closed) - wins
    longs  = [s for s in closed if s["direction"]=="LONG"]
    shorts = [s for s in closed if s["direction"]=="SHORT"]
    lw     = sum(1 for s in longs  if s["outcome"]=="WIN")
    sw     = sum(1 for s in shorts if s["outcome"]=="WIN")
    return {
        "total"      : len(closed),
        "wins"       : wins,
        "losses"     : losses,
        "win_rate"   : round(wins/len(closed)*100,1),
        "long_total" : len(longs),
        "long_wins"  : lw,
        "long_rate"  : round(lw/len(longs)*100,1) if longs else 0,
        "short_total": len(shorts),
        "short_wins" : sw,
        "short_rate" : round(sw/len(shorts)*100,1) if shorts else 0,
        "net_pnl_pct": round(sum(s.get("net_pnl_pct") or 0 for s in closed),2),
        "net_pnl_usd": round(sum(s.get("net_pnl_usd") or 0 for s in closed),2),
        "comm_pct"   : round(2*COMMISSION*100,2),
    }

_lock            = threading.Lock()
_state           = {}
_pending_signals = []   # RAM cache — DB'den yüklenir
_closed_signals  = []   # RAM cache — DB'den yüklenir

def load_signals():
    """DB'den pending + son 100 closed sinyali RAM'e yükle."""
    global _pending_signals, _closed_signals
    try:
        db_init()
        _pending_signals = db_load_pending()
        _closed_signals  = db_load_closed(limit=100)
        for s in _pending_signals + _closed_signals:
            if "symbol" not in s: s["symbol"] = SYMBOL
        print(f"[DB] Yüklendi: {len(_pending_signals)} bekleyen, {len(_closed_signals)} kapalı")
    except Exception as e:
        print(f"[DB HATA] Yükleme: {e}")
        _pending_signals = []; _closed_signals = []

def save_signals():
    """Artık DB'ye yazılıyor — bu fonksiyon geriye uyumluluk için kalıyor."""
    pass  # DB direkt yazılıyor, ayrıca flush gerekmez

_htf_cache       = {"trend":"NEUTRAL","ema_fast":0,"ema_slow":0,"rsi":50,"score":0,"details":[],"ts":None,"strength":0,"bull_sc":0,"bear_sc":0}
_htf_last_fetch  = 0
_mkt_cache = {
    "funding_rate":0.0,"funding_str":"bekleniyor",
    "oi_now":0.0,"oi_prev":0.0,"oi_change_pct":0.0,"oi_trend":"bekleniyor",
    "ls_ratio":1.0,"ls_str":"bekleniyor",
    "taker_buy":0.0,"taker_sell":0.0,"taker_ratio":1.0,"taker_str":"bekleniyor","ts":"—",
}
_mkt_last_fetch  = -999
_news_cache      = []
_news_last_fetch = -999
_tweet_cache     = []
_tweet_last_fetch = -999
_tweet_keywords  = [SYMBOL.split("/")[0]]

def fetch_ohlcv():
    raw = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=CANDLE_LIMIT)
    df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    return df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})

def calc_indicators(df):
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], EMA_FAST).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], EMA_SLOW).ema_indicator()
    df["rsi"]      = ta.momentum.RSIIndicator(df["close"], RSI_PERIOD).rsi()
    df["vol_ma"]   = df["volume"].rolling(20).mean()
    df["body"]     = df["close"] - df["open"]
    df["body_size"]= df["body"].abs()
    df["wick_up"]  = df["high"] - df[["open","close"]].max(axis=1)
    df["wick_down"]= df[["open","close"]].min(axis=1) - df["low"]
    return df

def fetch_htf_ohlcv():
    raw = exchange.fetch_ohlcv(SYMBOL, HTF_TIMEFRAME, limit=HTF_LIMIT)
    df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    return df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})

def calc_htf_trend(df_htf):
    df = df_htf.copy()
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], HTF_EMA_FAST).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], HTF_EMA_SLOW).ema_indicator()
    df["rsi"]      = ta.momentum.RSIIndicator(df["close"], 14).rsi()
    c=df.iloc[-1]; p2=df.iloc[-4]
    bull_score=0; bear_score=0; details=[]
    if c["ema_fast"]>c["ema_slow"]: bull_score+=1; details.append({"label":f"1h EMA{HTF_EMA_FAST}>EMA{HTF_EMA_SLOW}","side":"bull"})
    else: bear_score+=1; details.append({"label":f"1h EMA{HTF_EMA_FAST}<EMA{HTF_EMA_SLOW}","side":"bear"})
    slope=c["ema_slow"]-p2["ema_slow"]
    if slope>0: bull_score+=1; details.append({"label":f"1h EMA{HTF_EMA_SLOW} yukarı","side":"bull"})
    else: bear_score+=1; details.append({"label":f"1h EMA{HTF_EMA_SLOW} aşağı","side":"bear"})
    rsi=c["rsi"]
    if rsi>HTF_RSI_OB: bull_score+=1; details.append({"label":f"1h RSI bullish ({rsi:.0f})","side":"bull"})
    elif rsi<HTF_RSI_OS: bear_score+=1; details.append({"label":f"1h RSI bearish ({rsi:.0f})","side":"bear"})
    else: details.append({"label":f"1h RSI nötr ({rsi:.0f})","side":"neutral"})
    if c["close"]>c["ema_slow"]: bull_score+=1; details.append({"label":f"1h fiyat EMA{HTF_EMA_SLOW} üstünde","side":"bull"})
    else: bear_score+=1; details.append({"label":f"1h fiyat EMA{HTF_EMA_SLOW} altında","side":"bear"})
    if bull_score>=3: trend="BULL"; strength=bull_score
    elif bear_score>=3: trend="BEAR"; strength=bear_score
    else: trend="NEUTRAL"; strength=max(bull_score,bear_score)
    return {"trend":trend,"strength":strength,"bull_sc":bull_score,"bear_sc":bear_score,
            "ema_fast":round(float(c["ema_fast"]),2),"ema_slow":round(float(c["ema_slow"]),2),
            "rsi":round(float(rsi),1),"details":details,"ts":datetime.now().strftime("%H:%M:%S")}

def _get(path, params=None, timeout=5):
    r=requests.get(BNFUT_BASE+path,params=params,timeout=timeout); r.raise_for_status(); return r.json()

def fetch_market_data():
    sym="BTCUSDT"; result=dict(_mkt_cache)
    try:
        data=_get("/fapi/v1/premiumIndex",{"symbol":sym}); fr=float(data["lastFundingRate"])
        fr_str=f"aşırı LONG ({fr*100:.4f}%)" if fr>FUND_STRONG else f"aşırı SHORT ({fr*100:.4f}%)" if fr<FUND_WEAK else f"hafif {'pozitif' if fr>0 else 'negatif'} ({fr*100:.4f}%)"
        result["funding_rate"]=round(fr,6); result["funding_str"]=fr_str
    except Exception as e: print(f"[MKT/Funding] {e}")
    try:
        oi_now=float(_get("/fapi/v1/openInterest",{"symbol":sym})["openInterest"])
        history=_get("/futures/data/openInterestHist",{"symbol":sym,"period":"5m","limit":6})
        oi_prev=float(history[0]["sumOpenInterest"]) if history else oi_now
        oi_chg=(oi_now-oi_prev)/oi_prev if oi_prev>0 else 0
        oi_trend="nötr"
        if abs(oi_chg)>=OI_CHANGE_THR: oi_trend="artıyor" if oi_chg>0 else "azalıyor"
        result.update({"oi_now":round(oi_now,0),"oi_prev":round(oi_prev,0),"oi_change_pct":round(oi_chg*100,3),"oi_trend":oi_trend})
    except Exception as e: print(f"[MKT/OI] {e}")
    try:
        ls_data=_get("/futures/data/globalLongShortAccountRatio",{"symbol":sym,"period":"5m","limit":1})
        ls=float(ls_data[0]["longShortRatio"]) if ls_data else 1.0
        ls_str=f"kalabalık LONG ({ls:.2f})" if ls>LS_CROWD_LONG else f"kalabalık SHORT ({ls:.2f})" if ls<LS_CROWD_SHORT else f"dengeli ({ls:.2f})"
        result["ls_ratio"]=round(ls,3); result["ls_str"]=ls_str
    except Exception as e: print(f"[MKT/LS] {e}")
    try:
        klines=_get("/fapi/v1/klines",{"symbol":sym,"interval":"5m","limit":6})
        if klines:
            tv=sum(float(k[5]) for k in klines); tb=sum(float(k[9]) for k in klines); ts=tv-tb
            tk=tb/ts if ts>0 else 1.0
            tk_str=f"agresif alıcılar ({tk:.2f})" if tk>TAKER_STRONG else f"agresif satıcılar ({tk:.2f})" if tk<1/TAKER_STRONG else f"dengeli ({tk:.2f})"
            result.update({"taker_buy":round(tb,2),"taker_sell":round(ts,2),"taker_ratio":round(tk,3),"taker_str":tk_str})
    except Exception as e: print(f"[MKT/Taker] {e}")
    result["ts"]=datetime.now().strftime("%H:%M:%S")
    return result

def score_market_data(direction):
    mkt=_mkt_cache; is_long=direction=="LONG"; bonus=0; checks=[]; hard=False
    fr=mkt["funding_rate"]
    if is_long:
        if fr>FUND_STRONG: hard=True; checks.append({"label":f"🚫 Funding yüksek ({fr*100:.4f}%)","status":"fail","side":"short"})
        elif fr<FUND_WEAK: bonus+=1; checks.append({"label":f"✓ Funding negatif ({fr*100:.4f}%)","status":"pass","side":"long"})
        else: checks.append({"label":f"· Funding nötr ({fr*100:.4f}%)","status":"warn","side":"neutral"})
    else:
        if fr<FUND_WEAK: hard=True; checks.append({"label":f"🚫 Funding negatif ({fr*100:.4f}%)","status":"fail","side":"long"})
        elif fr>FUND_STRONG: bonus+=1; checks.append({"label":f"✓ Funding pozitif ({fr*100:.4f}%)","status":"pass","side":"short"})
        else: checks.append({"label":f"· Funding nötr ({fr*100:.4f}%)","status":"warn","side":"neutral"})
    oi_chg=mkt["oi_change_pct"]; oi_trend=mkt["oi_trend"]
    if oi_trend=="azalıyor": hard=True; checks.append({"label":f"🚫 OI azalıyor ({oi_chg:.2f}%)","status":"fail","side":""})
    elif oi_trend=="artıyor": bonus+=1; side="long" if is_long else "short"; checks.append({"label":f"✓ OI artıyor +{oi_chg:.2f}%","status":"pass","side":side})
    else: checks.append({"label":f"· OI nötr ({oi_chg:+.2f}%)","status":"warn","side":"neutral"})
    ls=mkt["ls_ratio"]
    if is_long:
        if ls>LS_CROWD_LONG: hard=True; checks.append({"label":f"🚫 Kalabalık LONG ({ls:.2f})","status":"fail","side":"short"})
        elif ls<LS_CROWD_SHORT: bonus+=1; checks.append({"label":f"✓ Kalabalık SHORT → LONG ({ls:.2f})","status":"pass","side":"long"})
        else: checks.append({"label":f"· L/S dengeli ({ls:.2f})","status":"warn","side":"neutral"})
    else:
        if ls<LS_CROWD_SHORT: hard=True; checks.append({"label":f"🚫 Kalabalık SHORT ({ls:.2f})","status":"fail","side":"long"})
        elif ls>LS_CROWD_LONG: bonus+=1; checks.append({"label":f"✓ Kalabalık LONG → SHORT ({ls:.2f})","status":"pass","side":"short"})
        else: checks.append({"label":f"· L/S dengeli ({ls:.2f})","status":"warn","side":"neutral"})
    tk=mkt["taker_ratio"]
    if is_long:
        if tk<1/TAKER_STRONG: hard=True; checks.append({"label":f"🚫 Agresif satıcılar (×{tk:.2f})","status":"fail","side":"short"})
        elif tk>TAKER_STRONG: bonus+=1; checks.append({"label":f"✓ Agresif alıcılar (×{tk:.2f})","status":"pass","side":"long"})
        else: checks.append({"label":f"· Taker dengeli (×{tk:.2f})","status":"warn","side":"neutral"})
    else:
        if tk>TAKER_STRONG: hard=True; checks.append({"label":f"🚫 Agresif alıcılar (×{tk:.2f})","status":"fail","side":"long"})
        elif tk<1/TAKER_STRONG: bonus+=1; checks.append({"label":f"✓ Agresif satıcılar (×{tk:.2f})","status":"pass","side":"short"})
        else: checks.append({"label":f"· Taker dengeli (×{tk:.2f})","status":"warn","side":"neutral"})
    return bonus, checks, hard

_RSS_HEADERS={"User-Agent":"Mozilla/5.0 (compatible; BTC-Dashboard/1.0)","Accept":"application/rss+xml,*/*"}

def _parse_rss_time(s):
    if not s: return ""
    from datetime import timezone
    for fmt in ["%a, %d %b %Y %H:%M:%S %z","%a, %d %b %Y %H:%M:%S %Z","%Y-%m-%dT%H:%M:%S%z","%Y-%m-%dT%H:%M:%SZ"]:
        try:
            dt=datetime.strptime(s.strip(),fmt)
            return dt.astimezone(timezone.utc).strftime("%H:%M")
        except: pass
    return s[:5] if len(s)>=5 else s

def _strip_html(text):
    text=html_lib.unescape(text or "")
    return re.sub(r"<[^>]+>","",text).strip()[:220]

def fetch_news():
    items=[]; sym_base=SYMBOL.split("/")[0]
    for feed in NEWS_FEEDS:
        try:
            url=feed["url"]
            if feed.get("dynamic"):
                q=requests.utils.quote(f"{sym_base} crypto price")
                url=f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
            r=requests.get(url,headers=_RSS_HEADERS,timeout=8); r.raise_for_status()
            root=ET.fromstring(r.content)
            for item in root.findall(".//item")[:10]:
                title=_strip_html(item.findtext("title",""))
                link=(item.findtext("link") or "").strip()
                pubdate=_parse_rss_time(item.findtext("pubDate",""))
                raw=item.findtext("pubDate","") or ""
                if "news.google.com" in link: title=re.sub(r"\s*-\s*[^-]+$","",title).strip()
                if title: items.append({"title":title,"source":feed["name"],"url":link,"ts":pubdate,"raw_ts":raw})
        except Exception as e: print(f"[RSS/{feed['name']}] {e}")
    items.sort(key=lambda x:x.get("raw_ts",""),reverse=True)
    seen,unique=set(),[]
    for it in items:
        k=it["title"][:50]
        if k not in seen: seen.add(k); unique.append(it)
    return unique[:NEWS_MAX]

def fetch_social(keywords):
    items=[]; sym_base=SYMBOL.split("/")[0]
    st_sym=STOCKTWITS_MAP.get(sym_base,sym_base+".X")
    try:
        url=f"https://api.stocktwits.com/api/2/streams/symbol/{st_sym}.json"
        r=requests.get(url,timeout=8,headers={"User-Agent":"btc-dashboard/1.0"}); r.raise_for_status()
        data=r.json()
        for msg in data.get("messages",[])[:TWEET_MAX]:
            body=_strip_html(msg.get("body",""))
            user=msg.get("user",{}).get("username","?")
            created=msg.get("created_at","")
            sentiment=msg.get("entities",{}).get("sentiment",{})
            sent_str=" 🟢" if sentiment.get("basic")=="Bullish" else " 🔴" if sentiment.get("basic")=="Bearish" else ""
            ts_fmt=""; raw_ts=created
            if created:
                try:
                    from datetime import timezone
                    dt=datetime.strptime(created,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    ts_fmt=dt.strftime("%H:%M")
                except: ts_fmt=created[:5]
            if body: items.append({"text":body+sent_str,"user":"@"+user,"url":f"https://stocktwits.com/{user}","ts":ts_fmt,"raw_ts":raw_ts,"score":0,"comms":0})
        print(f"[StockTwits] {len(items)} mesaj ({st_sym})")
    except Exception as e:
        print(f"[StockTwits] {e}")
        try:
            q=sym_base if not keywords else " OR ".join(keywords[:3])
            sub="+".join(REDDIT_SUBS[:4])
            r=requests.get(f"https://www.reddit.com/r/{sub}/search.json",headers=REDDIT_HEADERS,
                          params={"q":q,"sort":"new","limit":20,"t":"day"},timeout=8)
            r.raise_for_status()
            posts=r.json().get("data",{}).get("children",[])
            for post in posts:
                p=post.get("data",{}); title=_strip_html(p.get("title",""))
                author=p.get("author",""); score=p.get("score",0); comms=p.get("num_comments",0)
                created=p.get("created_utc",0); link="https://reddit.com"+p.get("permalink","")
                ts_fmt=""; raw_ts=""
                if created:
                    from datetime import timezone
                    dt=datetime.utcfromtimestamp(created).replace(tzinfo=timezone.utc)
                    ts_fmt=dt.strftime("%H:%M"); raw_ts=dt.isoformat()
                if title: items.append({"text":title,"user":f"u/{author}","url":link,"ts":ts_fmt,"raw_ts":raw_ts,"score":score,"comms":comms})
            print(f"[Reddit fallback] {len(items)} post")
        except Exception as e2: print(f"[Reddit fallback] {e2}")
    items.sort(key=lambda x:x.get("raw_ts",""),reverse=True)
    return items[:TWEET_MAX]

def cluster_walls(orders,ref,n):
    buckets={}
    for price,qty in orders:
        b=round(price/(ref*BUCKET_PCT))*(ref*BUCKET_PCT); buckets[b]=buckets.get(b,0)+qty
    buckets={p:v for p,v in buckets.items() if v>=MIN_WALL_BTC}
    top=sorted(buckets.items(),key=lambda x:x[1],reverse=True)[:n]
    return sorted(top,key=lambda x:x[0])

def detect_candle(df,is_long):
    c=df.iloc[-1]; p=df.iloc[-2]
    bs=c["body_size"]; wd=c["wick_down"]; wu=c["wick_up"]; b=c["body"]
    if is_long:
        if wd>bs*2 and wu<bs*0.5: return "Hammer 🔨"
        if b>0 and p["body"]<0 and bs>abs(p["body"])*1.2: return "Bullish Engulfing 📈"
        if len(df)>=3:
            pp=df.iloc[-3]
            if pp["body"]<0 and abs(p["body"])<abs(pp["body"])*0.4 and b>0: return "Morning Star ⭐"
        if b>0 and wd>bs*1.5: return "Bullish Pin Bar 📌"
    else:
        if wu>bs*2 and wd<bs*0.5: return "Shooting Star 💫"
        if b<0 and p["body"]>0 and bs>p["body_size"]*1.2: return "Bearish Engulfing 📉"
        if len(df)>=3:
            pp=df.iloc[-3]
            if pp["body"]>0 and abs(p["body"])<abs(pp["body"])*0.4 and b<0: return "Evening Star ⭐"
        if b<0 and wu>bs*1.5: return "Bearish Pin Bar 📌"
    return None

def score_reversal(df,direction):
    score=0; checks=[]; c=df.iloc[-1]; p=df.iloc[-2]; is_long=direction=="LONG"
    cb=(p["ema_fast"]<p["ema_slow"]) and (c["ema_fast"]>c["ema_slow"])
    cs=(p["ema_fast"]>p["ema_slow"]) and (c["ema_fast"]<c["ema_slow"])
    ba=c["ema_fast"]>c["ema_slow"]
    if is_long and (cb or ba): score+=1; checks.append({"label":f"EMA{EMA_FAST}>{EMA_SLOW}"+(" kesişim" if cb else ""),"status":"pass","side":"long"})
    elif not is_long and (cs or not ba): score+=1; checks.append({"label":f"EMA{EMA_FAST}<{EMA_SLOW}"+(" kesişim" if cs else ""),"status":"pass","side":"short"})
    else: checks.append({"label":"EMA aleyhte","status":"fail","side":""})
    rsi=c["rsi"]; div=False
    if is_long and len(df)>=5:
        lp=df["low"].iloc[-5:]; lr=df["rsi"].iloc[-5:]
        if lp.iloc[-1]<=lp.min() and lr.iloc[-1]>lr.min(): div=True
    elif not is_long and len(df)>=5:
        hp=df["high"].iloc[-5:]; hr=df["rsi"].iloc[-5:]
        if hp.iloc[-1]>=hp.max() and hr.iloc[-1]<hr.max(): div=True
    if is_long and (rsi<RSI_OS or div): score+=1; checks.append({"label":f"RSI {'diverjans' if div else 'aşırı satım'} ({rsi:.0f})","status":"pass","side":"long"})
    elif not is_long and (rsi>RSI_OB or div): score+=1; checks.append({"label":f"RSI {'diverjans' if div else 'aşırı alım'} ({rsi:.0f})","status":"pass","side":"short"})
    else: checks.append({"label":f"RSI nötr ({rsi:.0f})","status":"fail","side":""})
    vr=(c["volume"]/c["vol_ma"]) if c["vol_ma"]>0 else 0
    if vr>=VOL_MULTIPLIER: score+=1; checks.append({"label":f"Hacim spike ×{vr:.1f}","status":"pass","side":"neutral"})
    else: checks.append({"label":f"Hacim normal ×{vr:.1f}","status":"fail","side":""})
    pat=detect_candle(df,is_long)
    if pat: score+=1; checks.append({"label":pat,"status":"pass","side":"long" if is_long else "short"})
    else: checks.append({"label":"Net formasyon yok","status":"fail","side":""})
    return score, checks

def generate_signals(price, bid_walls, ask_walls, df):
    """
    Sinyal üretimi — çakışma koruması:
    Aynı anda LONG ve SHORT üretilmez.
    Her iki taraf da değerlendirilir, sadece daha güçlü olan seçilir.
    Beraberlik halinde HTF trendiyle uyumlu olan kazanır.
    """
    htf = _htf_cache
    candidates = []  # tüm adaylar

    def _build(direction, wp, wv, dist):
        is_long = direction == "LONG"
        sc, ch  = score_reversal(df, direction)
        htf_ok  = htf["trend"] != ("BEAR" if is_long else "BULL")
        if is_long:
            if   htf["trend"] == "BULL":    ch.append({"label":f"1h BULL ✓ ({htf['strength']}/4)","status":"pass","side":"long"}); sc+=1
            elif htf["trend"] == "NEUTRAL": ch.append({"label":f"1h NÖTR ({htf['strength']}/4)","status":"warn","side":"neutral"})
            else: ch.append({"label":"1h BEAR — LONG engellendi","status":"fail","side":"short"})
        else:
            if   htf["trend"] == "BEAR":    ch.append({"label":f"1h BEAR ✓ ({htf['strength']}/4)","status":"pass","side":"short"}); sc+=1
            elif htf["trend"] == "NEUTRAL": ch.append({"label":f"1h NÖTR ({htf['strength']}/4)","status":"warn","side":"neutral"})
            else: ch.append({"label":"1h BULL — SHORT engellendi","status":"fail","side":"long"})
        mkt_sc, mkt_ch, mkt_hard = score_market_data(direction)
        sc += mkt_sc; ch += mkt_ch
        blocked     = not htf_ok or mkt_hard
        block_reason = []
        if not htf_ok:  block_reason.append(f"1h {htf['trend']}")
        if mkt_hard:    block_reason.append("piyasa verisi")
        tp = round(price * (1 + TP_PCT if is_long else 1 - TP_PCT), 2)
        sl = round(price * (1 - SL_PCT if is_long else 1 + SL_PCT), 2)
        rt = 2 * COMMISSION
        return {
            "dir": direction, "entry": price, "tp": tp, "sl": sl,
            "net_tp_pct": round((TP_PCT - rt)*100, 2),
            "net_sl_pct": round((SL_PCT + rt)*100, 2),
            "net_tp_usd": round(price*(TP_PCT - rt), 2),
            "net_sl_usd": round(price*(SL_PCT + rt), 2),
            "comm_usd":   round(price * rt, 2),
            "wall_price": wp, "wall_vol": round(wv, 2),
            "dist_pct":   round(dist*100, 3), "score": sc, "checks": ch,
            "htf_blocked": blocked, "htf_trend": htf["trend"],
            "block_reason": " + ".join(block_reason) if block_reason else "",
            "mkt_score": mkt_sc,
        }

    for wp, wv in bid_walls:
        if wp >= price: continue
        dist = (price - wp) / price
        if dist > PROXIMITY_PCT: continue
        sig = _build("LONG", wp, wv, dist)
        if not sig["htf_blocked"] and sig["score"] < MIN_SCORE: continue
        candidates.append(sig)

    for wp, wv in ask_walls:
        if wp <= price: continue
        dist = (wp - price) / price
        if dist > PROXIMITY_PCT: continue
        sig = _build("SHORT", wp, wv, dist)
        if not sig["htf_blocked"] and sig["score"] < MIN_SCORE: continue
        candidates.append(sig)

    if not candidates:
        return []

    # ── Çakışma koruması ──────────────────────────────────────
    # Aktif (bloklanmamış) sinyaller
    active  = [s for s in candidates if not s["htf_blocked"]]
    blocked = [s for s in candidates if s["htf_blocked"]]

    has_long  = any(s["dir"] == "LONG"  for s in active)
    has_short = any(s["dir"] == "SHORT" for s in active)

    if has_long and has_short:
        best_long  = max((s for s in active if s["dir"]=="LONG"),  key=lambda x: x["score"])
        best_short = max((s for s in active if s["dir"]=="SHORT"), key=lambda x: x["score"])

        htf_trend = htf["trend"]
        if   htf_trend == "BULL":
            winner = best_long
        elif htf_trend == "BEAR":
            winner = best_short
        elif best_long["mkt_score"] != best_short["mkt_score"]:
            winner = best_long if best_long["mkt_score"] > best_short["mkt_score"] else best_short
        elif best_long["score"] != best_short["score"]:
            winner = best_long if best_long["score"] > best_short["score"] else best_short
        elif best_long["dist_pct"] != best_short["dist_pct"]:
            winner = best_long if best_long["dist_pct"] < best_short["dist_pct"] else best_short
        else:
            # Tam beraberlik — RSI yönüne bak
            rsi = float(df.iloc[-1]["rsi"]) if "rsi" in df.columns else 50
            winner = best_long if rsi < 50 else best_short

        loser = best_short if winner["dir"] == "LONG" else best_long
        print(f"[ÇAKIŞMA] {winner['dir']} ★{winner['score']} kazandı, {loser['dir']} ★{loser['score']} iptal | HTF:{htf_trend}")
        active = [winner]

    signals = active + blocked
    signals.sort(key=lambda x: (x["htf_blocked"], -x["score"]))

    # Pending'de zaten takip edilen yönleri işaretle
    pending_dirs = {s["dir"] for s in _pending_signals if s.get("symbol") == SYMBOL}
    for sig in signals:
        sig["already_tracked"] = sig["dir"] in pending_dirs

    return signals


def _close_signal(sig, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price=None, close_reason=None):
    global _closed_signals
    duration_min = sig.get("_duration_override")
    if duration_min is None:
        try:
            open_dt  = datetime.strptime(sig.get("ts","")[:8], "%H:%M:%S")
            close_dt = datetime.strptime(close_ts[:8], "%H:%M:%S")
            duration_min = max(0, int((close_dt - open_dt).seconds / 60))
        except Exception:
            duration_min = None

    closed = {**sig, "outcome": outcome, "net_pnl_pct": net_pnl_pct,
              "net_pnl_usd": net_pnl_usd, "close_ts": close_ts,
              "exit_price": exit_price, "duration_min": duration_min,
              "close_reason": close_reason}
    _closed_signals.append(closed)
    _closed_signals = _closed_signals[-100:]

    db_id = sig.get("_db_id") or sig.get("id")
    if db_id:
        try:
            db_close_signal(db_id, outcome, net_pnl_pct, net_pnl_usd,
                            close_ts, exit_price, duration_min, close_reason)
        except Exception as e:
            print(f"[DB HATA] close_signal: {e}")
    print(f"[SİNYAL] {sig['dir']} {outcome} | {close_reason or '?'} | PnL:{net_pnl_pct}% | Çıkış:{exit_price}")

def check_pending_signals(df):
    """
    Her döngüde bekleyen sinyalleri kontrol et.
    Yalnızca son kapanmış muma bakılır (entry_candle_idx mantığı kaldırıldı).
    Timeout YOK — sinyal TP veya SL'e ulaşana kadar açık kalır.
    """
    global _pending_signals
    still_pending = []
    rt     = 2 * COMMISSION
    now_ts = datetime.now().strftime("%H:%M:%S")

    # Son kapanmış mum (en güncel)
    last = df.iloc[-1]

    for sig in _pending_signals:
        is_long = sig["dir"] == "LONG"
        tp_hit  = last["high"] >= sig["tp"] if is_long else last["low"]  <= sig["tp"]
        sl_hit  = last["low"]  <= sig["sl"] if is_long else last["high"] >= sig["sl"]

        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                # Aynı mumda ikisi de vurdu — kapanışa göre karar ver
                outcome = "WIN"  if (is_long and last["close"] > sig["entry"]) or \
                                    (not is_long and last["close"] < sig["entry"]) else "LOSS"
            elif tp_hit:
                outcome = "WIN"
            else:
                outcome = "LOSS"

            net_pnl_pct = round((TP_PCT - rt)*100, 2) if outcome == "WIN" \
                          else -round((SL_PCT + rt)*100, 2)
            net_pnl_usd = round(sig["entry"]*(TP_PCT - rt), 2) if outcome == "WIN" \
                          else -round(sig["entry"]*(SL_PCT + rt), 2)
            exit_price  = sig["tp"] if outcome == "WIN" else sig["sl"]
            reason      = "TP hedefine ulaştı ✓" if outcome == "WIN" else "SL tetiklendi ✗"
            _close_signal(sig, outcome, net_pnl_pct, net_pnl_usd, now_ts, exit_price, reason)
        else:
            still_pending.append(sig)

    _pending_signals = still_pending



def calc_win_stats(symbol=None):
    """DB'den direkt hesapla."""
    return db_win_stats(symbol or SYMBOL)



def background_loop():
    global _pending_signals, _htf_cache, _htf_last_fetch, _mkt_cache, _mkt_last_fetch
    global _news_cache, _news_last_fetch, _tweet_cache, _tweet_last_fetch
    while True:
        try:
            now=time.time()
            if now-_htf_last_fetch>=HTF_REFRESH:
                try:
                    _htf_cache=calc_htf_trend(fetch_htf_ohlcv()); _htf_last_fetch=now
                    print(f"[HTF] {_htf_cache['trend']} bull={_htf_cache['bull_sc']} bear={_htf_cache['bear_sc']}")
                except Exception as e: print(f"[HTF Hata] {e}")
            if now-_mkt_last_fetch>=MKT_REFRESH:
                try: _mkt_cache=fetch_market_data(); _mkt_last_fetch=now; print(f"[MKT] FR={_mkt_cache['funding_rate']*100:.4f}% OI={_mkt_cache['oi_trend']} L/S={_mkt_cache['ls_ratio']} Taker={_mkt_cache['taker_ratio']:.2f}")
                except Exception as e: print(f"[MKT Hata] {e}")
            if now-_news_last_fetch>=NEWS_REFRESH:
                try: _news_cache=fetch_news(); _news_last_fetch=now; print(f"[NEWS] {len(_news_cache)} haber")
                except Exception as e: print(f"[NEWS Hata] {e}")
            if now-_tweet_last_fetch>=TWEET_REFRESH:
                try: _tweet_cache=fetch_social(_tweet_keywords); _tweet_last_fetch=now; print(f"[Social] {len(_tweet_cache)} post")
                except Exception as e: print(f"[Social Hata] {e}")
            ticker=exchange.fetch_ticker(SYMBOL); price=float(ticker["last"]); change24h=float(ticker.get("percentage",0) or 0)
            ob=exchange.fetch_order_book(SYMBOL,OB_DEPTH); df=fetch_ohlcv(); df=calc_indicators(df)
            bid_walls=cluster_walls(ob["bids"],price,TOP_WALLS); ask_walls=cluster_walls(ob["asks"],price,TOP_WALLS)
            signals=generate_signals(price,bid_walls,ask_walls,df); c=df.iloc[-1]
            candles_df=df.tail(60)[["open","high","low","close","volume","ema_fast","ema_slow","rsi","vol_ma"]].copy()
            candles_df[["ema_fast","ema_slow","rsi","vol_ma"]]=candles_df[["ema_fast","ema_slow","rsi","vol_ma"]].fillna(0)
            candles=candles_df.round(2).values.tolist()
            for sig in signals:
                if sig["htf_blocked"]:
                    continue  # engellenen sinyaller pending'e girmez

                open_for_symbol = [p for p in _pending_signals if p.get("symbol") == SYMBOL]
                same_dir_open   = [p for p in open_for_symbol if p["dir"] == sig["dir"]]
                opp_dir_open    = [p for p in open_for_symbol if p["dir"] != sig["dir"]]

                # Aynı yönde zaten açık sinyal varsa atla
                if same_dir_open:
                    continue

                # Karşı yönde açık sinyal varsa → koşullu kapat
                if opp_dir_open:
                    existing  = opp_dir_open[0]
                    is_long   = existing["dir"] == "LONG"
                    rt        = 2 * COMMISSION
                    raw_pct   = (price - existing["entry"]) / existing["entry"] * (1 if is_long else -1)
                    net_pct   = round((raw_pct - rt) * 100, 2)
                    net_usd   = round(existing["entry"] * (raw_pct - rt), 2)
                    outcome   = "WIN" if net_pct > 0 else "LOSS"

                    # Kapatma kararı kriterleri:
                    # 1. HTF trend tersine döndüyse → kesinlikle kapat
                    # 2. Yeni sinyal skoru > mevcut + 1 → daha güçlü sinyal, geç
                    # 3. Piyasa verisi sert karşı blok oluşturduysa → kapat
                    htf_reversed = (
                        (existing["dir"] == "LONG"  and _htf_cache["trend"] == "BEAR") or
                        (existing["dir"] == "SHORT" and _htf_cache["trend"] == "BULL")
                    )
                    stronger_signal = sig["score"] > existing.get("score", 0) + 1
                    mkt_against     = sig["mkt_score"] >= 2  # piyasa yeni yönü destekliyor

                    should_reverse = htf_reversed or (stronger_signal and mkt_against)

                    if not should_reverse:
                        # Şartlar net değil — mevcut sinyali koru
                        continue

                    reason = "HTF ters döndü" if htf_reversed else f"güçlü karşı sinyal (★{sig['score']})"
                    now_ts = datetime.now().strftime("%H:%M:%S")
                    _close_signal(existing, outcome, net_pct, net_usd,
                                  now_ts, exit_price=price,
                                  close_reason=f"↩ Reverse — {reason}")
                    _pending_signals = [p for p in _pending_signals
                                       if not (p.get("symbol")==SYMBOL and p["dir"]==existing["dir"])]
                    print(f"[REVERSE] {existing['dir']} @ {existing['entry']} → {sig['dir']} | {reason} | P&L:{net_pct}%")

                # Yeni sinyali ekle
                new_sig = {**sig, "ts": datetime.now().strftime("%H:%M:%S"), "symbol": SYMBOL}
                try:
                    db_id = db_insert_signal(new_sig)
                    new_sig["_db_id"] = db_id
                except Exception as e:
                    print(f"[DB HATA] insert: {e}")
                    new_sig["_db_id"] = None
                _pending_signals.append(new_sig)
                print(f"[YENİ SİNYAL] {sig['dir']} @ {sig['entry']} ★{sig['score']} DB:{new_sig['_db_id']}")
            stats=calc_win_stats(SYMBOL)
            check_pending_signals(df)
            new_state={
                "ts":datetime.now().strftime("%H:%M:%S"),"symbol":SYMBOL,"price":price,
                "change24h":round(change24h,2),"rsi":round(float(c["rsi"]),1),
                "ema_fast":round(float(c["ema_fast"]),2),"ema_slow":round(float(c["ema_slow"]),2),
                "vol_ratio":round(float(c["volume"]/c["vol_ma"]) if c["vol_ma"]>0 else 0,2),
                "bid_walls":[{"price":round(p,2),"vol":round(v,2)} for p,v in bid_walls],
                "ask_walls":[{"price":round(p,2),"vol":round(v,2)} for p,v in ask_walls],
                "signals":signals,"candles":candles,"htf":_htf_cache,"mkt":_mkt_cache,
                "news":_news_cache[:25],"tweets":_tweet_cache[:20],"tweet_kw":_tweet_keywords,
                "pending":[{k:v for k,v in s.items() if k not in ("checks","entry_candle_idx","waited_count","_duration_override","_db_id")}
                           for s in _pending_signals[-10:] if s.get("symbol")==SYMBOL],
                "closed":list(reversed([s for s in _closed_signals[-20:] if s.get("symbol")==SYMBOL])),
                "stats":stats,
            }
            with _lock: _state.update(new_state)
        except Exception as e:
            print(f"[Hata] {e}")
            import traceback; traceback.print_exc()
        time.sleep(REFRESH_SEC)


HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>BTC/USDT — Sinyal Botu</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#080c0f;--bg2:#0d1318;--bg3:#111820;--border:#1e2d3a;
  --amber:#f0a500;--amber-dim:#7a5200;--green:#00d264;--red:#ff3d5a;
  --cyan:#00c8e0;--text:#c8d8e8;--text-dim:#4a6070;--mono:'IBM Plex Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;height:100%;overflow-x:hidden}
header{display:flex;align-items:center;gap:14px;padding:8px 16px;background:var(--bg2);
  border-bottom:2px solid var(--amber-dim);position:sticky;top:0;z-index:100;flex-wrap:wrap}
.logo{font-size:13px;font-weight:600;letter-spacing:.14em;color:var(--amber);text-transform:uppercase;white-space:nowrap}
.logo span{color:var(--text-dim);font-weight:300}
.hstat{display:flex;flex-direction:column;gap:1px;border-left:1px solid var(--border);padding-left:12px}
.hstat-label{font-size:9px;letter-spacing:.1em;color:var(--text-dim);text-transform:uppercase}
.hstat-val{font-size:13px;font-weight:500}
.price-big{font-size:20px;font-weight:600;color:var(--amber)}
.up{color:var(--green)!important}.down{color:var(--red)!important}.neu{color:var(--text-dim)!important}
.dot-live{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:blink 1.4s infinite;margin-left:auto}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.ts-label{font-size:10px;color:var(--text-dim)}

/* ── Filter Bar ── */
.filter-bar{display:flex;gap:0;background:var(--border);height:30px;flex-shrink:0}
.fb-item{flex:1;display:flex;align-items:center;gap:5px;padding:0 8px;background:var(--bg2);
  font-size:9px;letter-spacing:.06em;text-transform:uppercase;border-right:1px solid var(--border);
  white-space:nowrap;overflow:hidden}
.fb-item:last-child{border:none}
.fb-label{color:var(--text-dim);flex-shrink:0}
.fb-bar-bg{flex:1;height:3px;background:var(--border);border-radius:2px;overflow:hidden}
.fb-bar-fill{height:100%;border-radius:2px;transition:width .4s,background .4s}
.fb-val{min-width:36px;text-align:right;font-weight:500;font-size:9px}

/* ── Grid ── */
.grid{display:grid;grid-template-columns:300px 1fr 310px;grid-template-rows:1fr 230px;
  gap:1px;background:var(--border);height:calc(100vh - 80px)}
.bottom-bar{grid-column:1/-1;grid-row:2;display:grid;grid-template-columns:1fr 1fr;
  gap:1px;background:var(--border);overflow:hidden}
.bottom-pane{background:var(--bg2);padding:8px 12px;overflow-y:auto;display:flex;flex-direction:column}
.bottom-title{font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:var(--text-dim);
  margin-bottom:6px;display:flex;align-items:center;gap:6px;flex-shrink:0}
.bottom-title::after{content:'';flex:1;height:1px;background:var(--border)}

.panel{background:var(--bg2);padding:10px 12px;overflow:hidden}
.panel-title{font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:var(--text-dim);
  margin-bottom:8px;display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* Order Book */
.ob-table{width:100%;border-collapse:collapse}
.ob-table td{padding:2px 3px;font-size:11px;white-space:nowrap;border-bottom:1px solid rgba(255,255,255,.02)}
.bar-bg{height:5px;border-radius:2px;background:var(--border);overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .4s}
.bar-ask{background:var(--red)}.bar-bid{background:var(--green)}
.ob-divider td{padding:4px 3px;font-size:12px;font-weight:600;color:var(--amber);
  background:var(--bg3);border-top:1px solid var(--amber-dim);border-bottom:1px solid var(--amber-dim)}

/* Indicator cards */
.ind-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border)}
.ind-card{background:var(--bg2);padding:8px 10px;display:flex;flex-direction:column;gap:2px}
.ind-label{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-dim)}
.ind-value{font-size:15px;font-weight:500}
.rsi-bar-bg{height:3px;background:var(--bg3);border-radius:2px;margin-top:3px;position:relative}
.rsi-bar-fill{position:absolute;top:0;left:0;height:100%;border-radius:2px;transition:width .5s}
.rsi-zone-ob{position:absolute;right:35%;top:-2px;bottom:-2px;width:1px;background:var(--red);opacity:.5}
.rsi-zone-os{position:absolute;left:35%;top:-2px;bottom:-2px;width:1px;background:var(--green);opacity:.5}

/* Chart */
.chart-panel{grid-column:2;grid-row:1;display:flex;flex-direction:column;gap:1px;overflow:hidden}
.chart-wrap{background:var(--bg2);padding:10px 12px 0;flex:1;display:flex;flex-direction:column;overflow:hidden}
#chart-container{flex:1;position:relative;overflow:hidden;min-height:200px}
#price-chart{position:absolute;top:0;left:0;width:100%;height:100%}

/* Win Rate */
.wr-panel{background:var(--bg3);border-radius:5px;padding:10px;margin-bottom:8px}
.wr-main{display:flex;align-items:baseline;gap:8px;margin-bottom:8px}
.wr-pct{font-size:32px;font-weight:600}
.wr-bar-wrap{height:5px;background:var(--border);border-radius:3px;margin:6px 0;overflow:hidden}
.wr-bar-fill{height:100%;border-radius:3px;background:var(--green);transition:width .6s}
.wr-split{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.wr-side{background:var(--bg2);border-radius:4px;padding:7px 9px}
.wr-side-label{font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.1em}
.wr-side-val{font-size:14px;font-weight:600;margin-top:2px}
.wr-side-sub{font-size:9px;color:var(--text-dim);margin-top:1px}

/* Signals */
.signal-box{background:var(--bg3);border-radius:4px;padding:8px 10px;margin-bottom:6px;border-left:3px solid var(--border)}
.signal-box.long{border-left-color:var(--green)}.signal-box.short{border-left-color:var(--red)}
.sig-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.sig-dir{font-size:12px;font-weight:600}.sig-score{font-size:10px;color:var(--text-dim)}
.sig-levels{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3px;margin-bottom:5px}
.sig-level{font-size:10px}.sig-level-label{color:var(--text-dim);font-size:9px;text-transform:uppercase}
.checks{display:flex;flex-direction:column;gap:1px}
.check-item{font-size:10px;display:flex;align-items:center;gap:4px}
.stars{letter-spacing:2px;font-size:11px}.star-fill{color:var(--amber)}.star-empty{color:var(--border)}
.no-signal{color:var(--text-dim);font-size:11px;text-align:center;padding:16px 0;border:1px dashed var(--border);border-radius:4px}
.result-row{display:flex;flex-wrap:wrap;gap:4px;align-items:center;padding:3px 0;border-bottom:1px solid var(--border);font-size:10px}
.result-row:last-child{border:none}
.badge{font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px}
.badge.win{background:rgba(0,210,100,.15);color:var(--green)}
.badge.loss{background:rgba(255,61,90,.15);color:var(--red)}
.badge.pending{background:rgba(240,165,0,.1);color:var(--amber)}
.pending-row{display:flex;flex-wrap:wrap;gap:4px;align-items:center;padding:3px 0;border-bottom:1px solid var(--border);font-size:10px}
.pending-row:last-child{border:none}

/* Dec rows */
.dec-row{display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.03)}
.dec-row:last-child{border:none}
.dec-icon{font-size:12px;width:14px;text-align:center;flex-shrink:0}
.dec-body{flex:1;min-width:0}
.dec-label{font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.07em}
.dec-val{font-size:10px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dec-bar-wrap{width:36px;height:3px;background:var(--border);border-radius:2px;flex-shrink:0;position:relative}
.dec-bar{height:100%;border-radius:2px;transition:width .5s,background .5s}
.dec-pass{color:var(--green)}.dec-fail{color:var(--red)}.dec-warn{color:var(--amber)}.dec-neutral{color:var(--text-dim)}

/* News / Tweets */
.news-item{padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer}
.news-item:hover .news-title{color:var(--amber)}
.news-source{font-size:9px;color:var(--text-dim);letter-spacing:.07em;text-transform:uppercase;margin-bottom:1px}
.news-title{font-size:10px;color:var(--text);line-height:1.4;transition:color .15s}
.news-time{font-size:9px;color:var(--text-dim);margin-top:1px}
.tweet-item{padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.tweet-user{font-size:9px;color:var(--cyan);margin-bottom:1px}
.tweet-text{font-size:10px;color:var(--text);line-height:1.4}
.tweet-time{font-size:9px;color:var(--text-dim);margin-top:1px;display:flex;gap:6px;align-items:center}
.kw-tag{display:inline-flex;align-items:center;gap:3px;padding:1px 6px;
  background:rgba(240,165,0,.1);border:1px solid var(--amber-dim);border-radius:3px;font-size:9px;color:var(--amber);cursor:pointer}

::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="logo" id="h-symbol-label">BTC<span>/USDT</span> · SİNYAL BOTU</div>
  <select id="symbol-select" onchange="changeSymbol(this.value)"
    style="background:var(--bg3);color:var(--amber);border:1px solid var(--amber-dim);
           border-radius:4px;padding:3px 7px;font-family:var(--mono);font-size:11px;cursor:pointer;outline:none">
    <option value="BTC/USDT">BTC/USDT</option>
    <option value="ETH/USDT">ETH/USDT</option>
    <option value="SOL/USDT">SOL/USDT</option>
    <option value="BNB/USDT">BNB/USDT</option>
    <option value="XRP/USDT">XRP/USDT</option>
    <option value="DOGE/USDT">DOGE/USDT</option>
  </select>
  <div class="hstat"><div class="hstat-label">Fiyat</div><div class="price-big" id="h-price">—</div></div>
  <div class="hstat"><div class="hstat-label">24s</div><div class="hstat-val" id="h-change">—</div></div>
  <div class="hstat"><div class="hstat-label">RSI</div><div class="hstat-val" id="h-rsi">—</div></div>
  <div class="hstat"><div class="hstat-label">EMA</div><div class="hstat-val" id="h-ema">—</div></div>
  <div class="hstat"><div class="hstat-label">Win Rate</div><div class="hstat-val" id="h-wr">—</div></div>
  <div class="hstat"><div class="hstat-label">Toplam</div><div class="hstat-val" id="h-total">—</div></div>
  <div class="hstat"><div class="hstat-label">1h HTF</div><div class="hstat-val" id="h-htf">—</div></div>
  <div class="dot-live" id="dot"></div>
  <div class="ts-label" id="h-ts">--:--:--</div>
</header>

<div id="filter-bar" class="filter-bar">
  <div class="fb-item"><span class="fb-label">—</span></div>
</div>

<div class="grid">
  <!-- Sol -->
  <div class="panel" style="grid-row:1;overflow-y:auto">
    <div class="panel-title">Order Book Duvarları</div>
    <table class="ob-table"><tbody id="ob-body">
      <tr><td colspan="3" style="color:var(--text-dim);padding:12px 0;text-align:center">Yükleniyor…</td></tr>
    </tbody></table>

    <div class="panel-title" style="margin-top:12px">5m Teknik Göstergeler</div>
    <div id="tech-panel">
      <div class="dec-row" id="dec-ema"><div class="dec-icon" id="dec-ema-icon">·</div>
        <div class="dec-body"><div class="dec-label">EMA 9/21</div><div class="dec-val" id="dec-ema-val">—</div></div>
        <div class="dec-bar-wrap"><div class="dec-bar" id="dec-ema-bar"></div></div></div>
      <div class="dec-row" id="dec-rsi"><div class="dec-icon" id="dec-rsi-icon">·</div>
        <div class="dec-body"><div class="dec-label">RSI (14)</div><div class="dec-val" id="dec-rsi-val">—</div></div>
        <div class="dec-bar-wrap"><div class="dec-bar" id="dec-rsi-bar" style="width:50%"></div>
          <div style="position:absolute;left:35%;top:0;bottom:0;width:1px;background:var(--green);opacity:.4"></div>
          <div style="position:absolute;right:35%;top:0;bottom:0;width:1px;background:var(--red);opacity:.4"></div>
        </div></div>
      <div class="dec-row" id="dec-vol"><div class="dec-icon" id="dec-vol-icon">·</div>
        <div class="dec-body"><div class="dec-label">Hacim</div><div class="dec-val" id="dec-vol-val">—</div></div>
        <div class="dec-bar-wrap"><div class="dec-bar" id="dec-vol-bar"></div></div></div>
      <div class="dec-row" id="dec-candle"><div class="dec-icon" id="dec-candle-icon">·</div>
        <div class="dec-body"><div class="dec-label">Formasyon</div><div class="dec-val" id="dec-candle-val">—</div></div>
        <div class="dec-bar-wrap" style="width:16px"></div></div>
    </div>

    <div class="panel-title" style="margin-top:12px">1h HTF <span id="htf-badge"></span></div>
    <div id="htf-panel" style="background:var(--bg3);border-radius:4px;padding:7px 9px">
      <div style="color:var(--text-dim);font-size:11px">Yükleniyor…</div></div>

    <div class="panel-title" style="margin-top:12px">Piyasa Verisi <span id="mkt-ts" style="color:var(--text-dim);font-size:9px;margin-left:3px"></span></div>
    <div class="dec-row" id="dec-fr"><div class="dec-icon" id="dec-fr-icon">·</div>
      <div class="dec-body"><div class="dec-label">Funding Rate</div><div class="dec-val" id="dec-fr-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-fr-bar"></div></div></div>
    <div class="dec-row" id="dec-oi"><div class="dec-icon" id="dec-oi-icon">·</div>
      <div class="dec-body"><div class="dec-label">Open Interest</div><div class="dec-val" id="dec-oi-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-oi-bar"></div></div></div>
    <div class="dec-row" id="dec-ls"><div class="dec-icon" id="dec-ls-icon">·</div>
      <div class="dec-body"><div class="dec-label">Long / Short</div><div class="dec-val" id="dec-ls-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-ls-bar"></div></div></div>
    <div class="dec-row" id="dec-tk"><div class="dec-icon" id="dec-tk-icon">·</div>
      <div class="dec-body"><div class="dec-label">Taker Buy/Sell</div><div class="dec-val" id="dec-tk-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-tk-bar"></div></div></div>
  </div>

  <!-- Orta: Grafik -->
  <div class="chart-panel">
    <div class="ind-grid">
      <div class="ind-card">
        <div class="ind-label">RSI (14)</div><div class="ind-value" id="ind-rsi">—</div>
        <div class="rsi-bar-bg"><div class="rsi-bar-fill" id="rsi-fill" style="width:50%;background:var(--amber)"></div>
          <div class="rsi-zone-ob"></div><div class="rsi-zone-os"></div></div>
      </div>
      <div class="ind-card"><div class="ind-label">EMA Trend</div><div class="ind-value" id="ind-ema">—</div></div>
      <div class="ind-card"><div class="ind-label">Hacim Oranı</div><div class="ind-value" id="ind-vol">—</div></div>
    </div>
    <div class="chart-wrap">
      <div class="panel-title">5 Dakikalık Mum Grafiği</div>
      <div id="chart-container" style="position:relative;flex:1;min-height:200px;overflow:hidden">
        <canvas id="price-chart" style="position:absolute;top:0;left:0"></canvas>
      </div>
    </div>
  </div>

  <!-- Sağ -->
  <div class="panel" style="grid-row:1;overflow-y:auto">
    <div class="panel-title">Tutma Oranı</div>
    <div class="wr-panel" id="wr-panel">
      <div class="wr-main">
        <div class="wr-pct" id="wr-pct" style="color:var(--text-dim)">—</div>
        <div><div style="font-size:10px;color:var(--text-dim)">Win Rate</div><div style="font-size:10px;color:var(--text-dim)" id="wr-meta">Sinyal bekleniyor</div></div>
      </div>
      <div class="wr-bar-wrap"><div class="wr-bar-fill" id="wr-bar" style="width:0%"></div></div>
      <div id="wr-comm-info" style="font-size:9px;color:var(--text-dim);margin-top:4px">Komisyon: %0.15 × 2 = %0.30</div>
      <div class="wr-split">
        <div class="wr-side"><div class="wr-side-label">🟢 Long</div>
          <div class="wr-side-val" id="wr-long-rate" style="color:var(--green)">—</div>
          <div class="wr-side-sub" id="wr-long-meta">0 sinyal</div></div>
        <div class="wr-side"><div class="wr-side-label">🔴 Short</div>
          <div class="wr-side-val" id="wr-short-rate" style="color:var(--red)">—</div>
          <div class="wr-side-sub" id="wr-short-meta">0 sinyal</div></div>
      </div>
    </div>
    <div style="display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap">
      <button onclick="clearSignals('pending')"
        style="flex:1;font-size:9px;padding:3px 0;background:rgba(240,165,0,.1);color:var(--amber);
               border:1px solid var(--amber-dim);border-radius:3px;cursor:pointer">
        ✕ Bekleyenleri Sil
      </button>
      <button onclick="clearSignals('closed')"
        style="flex:1;font-size:9px;padding:3px 0;background:rgba(255,61,90,.08);color:var(--red);
               border:1px solid rgba(255,61,90,.3);border-radius:3px;cursor:pointer">
        ✕ Geçmişi Sil
      </button>
      <button onclick="clearSignals('all')"
        style="flex:1;font-size:9px;padding:3px 0;background:rgba(100,100,100,.1);color:var(--text-dim);
               border:1px solid var(--border);border-radius:3px;cursor:pointer">
        ✕ Tümünü Sil
      </button>
    </div>

    <div class="panel-title">Aktif Sinyaller</div>    <div id="signal-area"><div class="no-signal">⏳ Veri bekleniyor…</div></div>
    <div class="panel-title" style="margin-top:10px">Sonuç Bekleniyor <span id="pending-count" style="color:var(--amber)"></span></div>
    <div id="pending-area"><div style="color:var(--text-dim);font-size:10px;padding:6px 0">Bekleyen sinyal yok</div></div>
    <div class="panel-title" style="margin-top:10px">Kapanmış Sinyaller</div>
    <div id="closed-area"><div style="color:var(--text-dim);font-size:10px;padding:6px 0">Henüz kapanmadı</div></div>
  </div>

  <!-- Alt Bar -->
  <div class="bottom-bar">
    <div class="bottom-pane">
      <div class="bottom-title">📰 Haberler
        <span style="font-size:9px;color:var(--text-dim)">Google News + RSS</span>
        <button onclick="refreshNews()" style="margin-left:auto;font-size:9px;color:var(--amber);background:none;border:1px solid var(--amber-dim);border-radius:3px;padding:1px 6px;cursor:pointer">↻</button>
      </div>
      <div id="news-list" style="overflow-y:auto;flex:1">
        <div style="color:var(--text-dim);font-size:10px;text-align:center;padding:16px 0">Yükleniyor…</div>
      </div>
    </div>
    <div class="bottom-pane">
      <div class="bottom-title">📈 StockTwits <span id="st-sym-label" style="font-size:9px;color:var(--text-dim)"></span></div>
      <div style="display:flex;gap:4px;margin-bottom:4px;flex-shrink:0;align-items:center">
        <input id="kw-input" type="text" placeholder="ekstra filtre…"
          style="flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:3px;
                 padding:3px 6px;color:var(--text);font-family:var(--mono);font-size:10px;outline:none"
          onkeydown="if(event.key==='Enter')setKeywords()">
        <button onclick="setKeywords()" style="background:var(--amber-dim);color:var(--amber);border:none;border-radius:3px;padding:3px 8px;cursor:pointer;font-size:10px">Ara</button>
      </div>
      <div id="kw-tags" style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:3px;flex-shrink:0"></div>
      <div id="tweet-list" style="overflow-y:auto;flex:1">
        <div style="color:var(--text-dim);font-size:10px;text-align:center;padding:16px 0">Bekleniyor…</div>
      </div>
    </div>
  </div>
</div>

<script>
const PROXIMITY_PCT = __PROXIMITY__;
const TP_PCT        = __TP__;
const SL_PCT        = __SL__;
const COMMISSION    = __COMM__;
const EMA_FAST      = __EMA_FAST__;
const EMA_SLOW      = __EMA_SLOW__;
const HTF_EMA_FAST  = __HTF_EMA_FAST__;
const HTF_EMA_SLOW  = __HTF_EMA_SLOW__;

const cvs = document.getElementById('price-chart');
let prevPrice = null;
let tooltip   = {visible:false,x:0,y:0,candle:null};

// ── drawChart ─────────────────────────────────────────────
function drawChart(candles) {
  if (!candles || !candles.length) return;
  const container = document.getElementById('chart-container');
  const W = container.clientWidth || container.offsetWidth;
  const H = container.clientHeight || container.offsetHeight;
  if (!W || !H || W < 30 || H < 30) { setTimeout(()=>drawChart(candles),120); return; }
  cvs.width = W; cvs.height = H;
  const g = cvs.getContext('2d');

  const data = candles.slice(-60);
  const N = data.length;
  if (!N) return;

  const PL=6,PR=66,chartW=W-PL-PR;
  const HP=Math.floor(H*.62),HV=Math.floor(H*.13),HR=H-HP-HV-4;
  const YP=0,YV=HP+2,YR=HP+HV+4;
  const step=chartW/N, cw=Math.max(1,(step-2)|0);

  const lows=data.map(c=>+c[2]).filter(v=>v>0);
  const highs=data.map(c=>+c[1]).filter(v=>v>0);
  const efs=data.map(c=>+c[5]).filter(v=>v>0);
  const ess=data.map(c=>+c[6]).filter(v=>v>0);
  if (!lows.length) return;
  let lo=Math.min(...lows,...(efs.length?efs:[Infinity]));
  let hi=Math.max(...highs,...(ess.length?ess:[-Infinity]));
  if (!isFinite(lo)||!isFinite(hi)||lo>=hi){lo=lo-100;hi=hi+100;}
  const rng=hi-lo; lo-=rng*.04; hi+=rng*.06;
  const span=hi-lo;
  const toY=p=>YP+HP-((p-lo)/span)*HP;

  // Izgara
  g.strokeStyle='rgba(30,45,58,.7)'; g.lineWidth=.5;
  for(let i=0;i<=5;i++){
    const y=YP+HP/5*i;
    g.beginPath();g.moveTo(PL,y);g.lineTo(W-PR,y);g.stroke();
    g.fillStyle='#4a6070';g.font='10px monospace';g.textAlign='left';
    g.fillText('$'+(hi-span/5*i).toLocaleString('en-US',{maximumFractionDigits:0}),W-PR+4,y+4);
  }
  const ts=Math.max(1,(N/6)|0);
  for(let i=0;i<N;i+=ts){
    const x=PL+i*step+step/2;
    const dt=new Date(Date.now()-(N-1-i)*5*60000);
    g.strokeStyle='rgba(30,45,58,.3)';g.lineWidth=.5;
    g.beginPath();g.moveTo(x,YP);g.lineTo(x,YR+HR);g.stroke();
    g.fillStyle='#4a6070';g.font='9px monospace';g.textAlign='center';
    g.fillText(dt.getHours().toString().padStart(2,'0')+':'+dt.getMinutes().toString().padStart(2,'0'),x,H-2);
  }

  // EMA
  const drawEma=(ci,clr)=>{
    g.strokeStyle=clr;g.lineWidth=1.2;g.globalAlpha=.7;g.setLineDash([]);
    g.beginPath();let s=false;
    data.forEach((c,i)=>{const v=+c[ci];if(!v||v<=0)return;const x=PL+i*step+step/2,y=toY(v);s?g.lineTo(x,y):(g.moveTo(x,y),s=true);});
    g.stroke();g.globalAlpha=1;
  };
  drawEma(6,'#f0a500');drawEma(5,'#00c8e0');
  g.font='9px monospace';g.textAlign='left';
  g.fillStyle='#00c8e0';g.fillText('EMA'+EMA_FAST,PL+4,YP+13);
  g.fillStyle='#f0a500';g.fillText('EMA'+EMA_SLOW,PL+44,YP+13);

  // Mumlar
  g.globalAlpha=1;g.setLineDash([]);
  data.forEach((c,i)=>{
    const o=+c[0],h=+c[1],l=+c[2],cl=+c[3];
    if(!o||!h||!l||!cl||isNaN(o)||o<=0) return;
    const x=PL+i*step+step/2|0;
    const col=cl>=o?'#00d264':'#ff3d5a';
    const yH=toY(h)|0,yL=toY(l)|0,yO=toY(o),yC=toY(cl);
    const bt=Math.min(yO,yC)|0,bb=Math.max(yO,yC)|0,bh=Math.max(1,bb-bt);
    g.strokeStyle=col;g.lineWidth=1;
    g.beginPath();g.moveTo(x,yH);g.lineTo(x,bt);g.moveTo(x,bt+bh);g.lineTo(x,yL);g.stroke();
    g.fillStyle=col;g.fillRect(x-(cw>>1),bt,Math.max(1,cw),bh);
  });

  // Fiyat çizgisi
  const lc=+data[N-1][3];
  if(lc>0){
    const yl=toY(lc)|0;
    g.setLineDash([3,3]);g.strokeStyle='#f0a500';g.lineWidth=1;
    g.beginPath();g.moveTo(PL,yl);g.lineTo(W-PR,yl);g.stroke();g.setLineDash([]);
    g.fillStyle='#f0a500';g.fillRect(W-PR+1,yl-8,PR-3,16);
    g.fillStyle='#080c0f';g.font='10px monospace';g.textAlign='center';
    g.fillText('$'+lc.toLocaleString('en-US',{maximumFractionDigits:0}),W-PR+(PR-4)/2,yl+4);
  }

  // Hacim
  g.fillStyle='rgba(30,45,58,.1)';g.fillRect(PL,YV,chartW,HV);
  const maxV=Math.max(...data.map(c=>+c[4]||0),1);
  data.forEach((c,i)=>{
    const v=+c[4]||0,x=PL+i*step+step/2,bh=Math.max(1,(v/maxV)*HV);
    g.fillStyle=(+c[3]>=(+c[0]))?'rgba(0,210,100,.35)':'rgba(255,61,90,.35)';
    g.fillRect(x-cw/2,YV+HV-bh,Math.max(1,cw),bh);
  });
  g.strokeStyle='rgba(240,165,0,.45)';g.lineWidth=1;g.setLineDash([2,2]);
  g.beginPath();let vs=false;
  data.forEach((c,i)=>{const vm=+c[8];if(!vm)return;const x=PL+i*step+step/2,y=YV+HV-(vm/maxV)*HV;vs?g.lineTo(x,y):(g.moveTo(x,y),vs=true);});
  g.stroke();g.setLineDash([]);
  g.fillStyle='#4a6070';g.font='8px monospace';g.textAlign='left';g.fillText('VOL',W-PR+4,YV+10);

  // RSI
  g.fillStyle='rgba(30,45,58,.1)';g.fillRect(PL,YR,chartW,HR);
  const toYR=v=>YR+HR-(v/100)*HR;
  [[70,'rgba(255,61,90,.2)'],[50,'rgba(100,120,130,.15)'],[30,'rgba(0,210,100,.2)']].forEach(([v,c])=>{
    g.strokeStyle=c;g.lineWidth=.5;g.beginPath();g.moveTo(PL,toYR(v));g.lineTo(W-PR,toYR(v));g.stroke();
  });
  g.strokeStyle='#c070ff';g.lineWidth=1.2;g.beginPath();let rs=false;
  data.forEach((c,i)=>{const rv=+c[7];if(!rv)return;const x=PL+i*step+step/2,y=toYR(rv);rs?g.lineTo(x,y):(g.moveTo(x,y),rs=true);});
  g.stroke();
  const lr=+data[N-1][7]||0;
  g.fillStyle='#4a6070';g.font='8px monospace';g.textAlign='left';g.fillText('RSI',W-PR+4,YR+10);
  if(lr>0){g.fillStyle=lr>70?'#ff3d5a':lr<30?'#00d264':'#c070ff';g.font='9px monospace';g.fillText(lr.toFixed(1),W-PR+4,YR+22);}

  // Tooltip
  if(tooltip.visible&&tooltip.candle){
    const tc=tooltip.candle,tx=Math.min(tooltip.x+10,W-140),ty=Math.max(tooltip.y-95,4);
    const bull=+tc[3]>=+tc[0];
    g.fillStyle='rgba(11,17,22,.96)';g.strokeStyle='#1e2d3a';g.lineWidth=1;
    g.beginPath();g.roundRect(tx,ty,138,100,4);g.fill();g.stroke();
    g.font='10px monospace';g.textAlign='left';
    g.fillStyle=bull?'#00d264':'#ff3d5a';g.fillText(bull?'▲ Yükselen':'▼ Düşen',tx+8,ty+14);
    g.fillStyle='#c8d8e8';
    g.fillText('A:'+(+tc[0]).toLocaleString()+' K:'+(+tc[3]).toLocaleString(),tx+8,ty+27);
    g.fillText('Y:'+(+tc[1]).toLocaleString()+' D:'+(+tc[2]).toLocaleString(),tx+8,ty+40);
    g.fillStyle='#00c8e0';g.fillText('EMA'+EMA_FAST+': $'+(+tc[5]||0).toLocaleString(),tx+8,ty+53);
    g.fillStyle='#f0a500';g.fillText('EMA'+EMA_SLOW+': $'+(+tc[6]||0).toLocaleString(),tx+8,ty+66);
    g.fillStyle='#c070ff';g.fillText('RSI: '+(+tc[7]||0).toFixed(1),tx+8,ty+79);
    g.fillStyle='rgba(200,216,232,.5)';g.fillText('Vol: '+(+tc[4]||0).toLocaleString(),tx+8,ty+92);
  }
}

function initChart(candles){
  window._lastCandles=candles;
  let tries=0;
  const t=setInterval(()=>{
    const el=document.getElementById('chart-container');
    const w=el?(el.clientWidth||el.offsetWidth):0;
    const h=el?(el.clientHeight||el.offsetHeight):0;
    if((w>30&&h>30)||tries++>40){clearInterval(t);drawChart(candles);}
  },80);
}

new ResizeObserver(()=>{if(window._lastCandles)drawChart(window._lastCandles);})
  .observe(document.getElementById('chart-container'));

cvs.addEventListener('mousemove',e=>{
  const candles=window._lastCandles; if(!candles)return;
  const data=candles.slice(-60),N=data.length;
  const rect=cvs.getBoundingClientRect(),mx=e.clientX-rect.left,my=e.clientY-rect.top;
  const step=(cvs.width-6-66)/N,idx=Math.floor((mx-6)/step);
  tooltip=(idx>=0&&idx<N)?{visible:true,x:mx,y:my,candle:data[idx]}:{visible:false,x:0,y:0,candle:null};
  drawChart(candles);
});
cvs.addEventListener('mouseleave',()=>{tooltip={visible:false,x:0,y:0,candle:null};if(window._lastCandles)drawChart(window._lastCandles);});
window.addEventListener('resize',()=>{if(window._lastCandles)drawChart(window._lastCandles);});

// ── Helpers ──────────────────────────────────────────────
function stars(n,max=4){return`<span class="stars"><span class="star-fill">${'★'.repeat(n)}</span><span class="star-empty">${'☆'.repeat(max-n)}</span></span>`;}
function fmt(n){return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}
function escHtml(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function openLink(url){if(url)window.open(url,'_blank','noopener');}
function checkHtml(ch){
  const icon=ch.status==='pass'?'✓':'·';
  const color=ch.status==='pass'?(ch.side==='long'?'var(--green)':ch.side==='short'?'var(--red)':'var(--amber)'):'var(--text-dim)';
  return`<div class="check-item" style="color:${color}"><span style="width:12px;font-size:9px">${icon}</span><span>${escHtml(ch.label)}</span></div>`;
}

function setDecRow(id,status,valText,barPct,barClr){
  const icon=document.getElementById('dec-'+id+'-icon'),val=document.getElementById('dec-'+id+'-val'),bar=document.getElementById('dec-'+id+'-bar');
  const cfg={pass:{sym:'✓',cls:'dec-pass',bg:'var(--green)'},fail:{sym:'✗',cls:'dec-fail',bg:'var(--red)'},
             warn:{sym:'·',cls:'dec-warn',bg:'var(--amber)'},neutral:{sym:'─',cls:'dec-neutral',bg:'var(--text-dim)'}}[status]||{sym:'·',cls:'dec-neutral',bg:'var(--text-dim)'};
  if(icon){icon.textContent=cfg.sym;icon.className='dec-icon '+cfg.cls;}
  if(val){val.textContent=valText;val.className='dec-val '+cfg.cls;}
  if(bar){bar.style.width=(barPct||0)+'%';bar.style.background=barClr||cfg.bg;}
}

// ── Render functions ─────────────────────────────────────
function renderHeader(d){
  const pe=document.getElementById('h-price');if(!pe)return;
  const dir=prevPrice===null?'neu':d.price>prevPrice?'up':d.price<prevPrice?'down':'neu';
  pe.textContent='$'+fmt(d.price||0);pe.className='price-big '+dir;
  const ce=document.getElementById('h-change');
  if(ce){ce.textContent=(d.change24h>=0?'+':'')+d.change24h+'%';ce.className='hstat-val '+(d.change24h>=0?'up':'down');}
  const re=document.getElementById('h-rsi');
  if(re){re.textContent=d.rsi||'—';re.className='hstat-val '+(d.rsi>65?'down':d.rsi<35?'up':'');}
  const bull=(d.ema_fast||0)>(d.ema_slow||0);
  const emaEl=document.getElementById('h-ema');
  if(emaEl)emaEl.innerHTML=`<span class="${bull?'up':'down'}">${bull?'BULL ▲':'BEAR ▼'}</span>`;
  const st=d.stats||{total:0,wins:0,losses:0,win_rate:0};
  const wrEl=document.getElementById('h-wr');
  if(wrEl){if(st.total>0){wrEl.textContent=st.win_rate+'%';wrEl.style.color=st.win_rate>=55?'var(--green)':st.win_rate>=45?'var(--amber)':'var(--red)';}else{wrEl.textContent='—';wrEl.style.color='var(--text-dim)';}}
  const totEl=document.getElementById('h-total');if(totEl)totEl.textContent=st.total>0?`${st.wins}W/${st.losses}L`:'—';
  const tsEl=document.getElementById('h-ts');if(tsEl)tsEl.textContent=d.ts||'';
  const htfEl=document.getElementById('h-htf');
  if(htfEl&&d.htf&&d.htf.trend){const hc=d.htf.trend==='BULL'?'var(--green)':d.htf.trend==='BEAR'?'var(--red)':'var(--amber)';const hi=d.htf.trend==='BULL'?'▲':d.htf.trend==='BEAR'?'▼':'─';htfEl.innerHTML=`<span style="color:${hc}">${d.htf.trend} ${hi} ${d.htf.strength||0}/4</span>`;}
  prevPrice=d.price;
}

function renderIndicators(d){
  const re=document.getElementById('ind-rsi');if(!re)return;
  re.textContent=d.rsi;re.style.color=d.rsi>65?'var(--red)':d.rsi<35?'var(--green)':'var(--amber)';
  const fill=document.getElementById('rsi-fill');fill.style.width=d.rsi+'%';fill.style.background=d.rsi>65?'var(--red)':d.rsi<35?'var(--green)':'var(--amber)';
  const bull=d.ema_fast>d.ema_slow;
  document.getElementById('ind-ema').innerHTML=`<span style="color:${bull?'var(--green)':'var(--red)'}">${bull?'BULL ▲':'BEAR ▼'}</span>`;
  const ve=document.getElementById('ind-vol');ve.textContent='×'+d.vol_ratio;ve.style.color=d.vol_ratio>=1.8?'var(--amber)':'var(--text)';
}

function renderOrderBook(d){
  const price=d.price,maxVol=Math.max(...d.ask_walls.map(w=>w.vol),...d.bid_walls.map(w=>w.vol),1);
  let html='';
  [...d.ask_walls].reverse().forEach(w=>{const dist=((w.price-price)/price*100).toFixed(2);const bp=(w.vol/maxVol*100).toFixed(1);const near=parseFloat(dist)<=PROXIMITY_PCT*100;html+=`<tr><td style="color:var(--red)">$${fmt(w.price)}<small style="color:var(--text-dim);margin-left:4px">+${dist}%${near?' ⚡':''}</small></td><td style="color:var(--red);text-align:right">${w.vol.toFixed(1)}</td><td style="padding-left:4px"><div class="bar-bg"><div class="bar-fill bar-ask" style="width:${bp}%"></div></div></td></tr>`;});
  html+=`<tr class="ob-divider"><td>▶ $${fmt(price)}</td><td colspan="2" style="color:var(--text-dim);font-size:10px;text-align:right">SPOT</td></tr>`;
  [...d.bid_walls].reverse().forEach(w=>{const dist=((price-w.price)/price*100).toFixed(2);const bp=(w.vol/maxVol*100).toFixed(1);const near=parseFloat(dist)<=PROXIMITY_PCT*100;html+=`<tr><td style="color:var(--green)">$${fmt(w.price)}<small style="color:var(--text-dim);margin-left:4px">-${dist}%${near?' ⚡':''}</small></td><td style="color:var(--green);text-align:right">${w.vol.toFixed(1)}</td><td style="padding-left:4px"><div class="bar-bg"><div class="bar-fill bar-bid" style="width:${bp}%"></div></div></td></tr>`;});
  document.getElementById('ob-body').innerHTML=html;
}

function renderTech(d){
  if(d.ema_fast&&d.ema_slow){const bull=d.ema_fast>d.ema_slow;const diff=((d.ema_fast-d.ema_slow)/d.ema_slow*100).toFixed(3);setDecRow('ema',bull?'pass':'fail',bull?`${d.ema_fast.toLocaleString()} > ${d.ema_slow.toLocaleString()} (+${Math.abs(diff)}%)`:`${d.ema_fast.toLocaleString()} < ${d.ema_slow.toLocaleString()} (${diff}%)`,bull?75:25,bull?'var(--green)':'var(--red)');}
  if(d.rsi){const r=d.rsi,st=r>65?'fail':r<35?'pass':'warn',lbl=r>65?`${r} — aşırı alım`:r<35?`${r} — aşırı satım`:`${r} — nötr`;setDecRow('rsi',st,lbl,r,r>65?'var(--red)':r<35?'var(--green)':'var(--amber)');}
  if(d.vol_ratio!==undefined){const vr=d.vol_ratio,st=vr>=1.8?'pass':vr>=1.2?'warn':'neutral',pct=Math.min(100,vr/3*100);setDecRow('vol',st,`×${vr} ${vr>=1.8?'— spike!':vr>=1.2?'— yüksek':'— normal'}`,pct,vr>=1.8?'var(--amber)':'var(--green)');}
  const activeSig=(d.signals||[]).find(s=>!s.htf_blocked);
  const cc=activeSig&&(activeSig.checks||[]).find(c=>['Hammer','Engulfing','Star','Pin'].some(k=>c.label.includes(k)));
  if(cc)setDecRow('candle',cc.status==='pass'?'pass':'warn',cc.label,0);
  else setDecRow('candle','neutral','Formasyon bekleniyor',0);
}

function renderHTF(d){
  const panel=document.getElementById('htf-panel');if(!d.htf||!d.htf.trend)return;
  const htf=d.htf,clr=htf.trend==='BULL'?'var(--green)':htf.trend==='BEAR'?'var(--red)':'var(--amber)';
  const icon=htf.trend==='BULL'?'▲':htf.trend==='BEAR'?'▼':'─';
  const barW=(htf.strength/4*100).toFixed(0);
  const dHtml=(htf.details||[]).map(d=>{const dc=d.side==='bull'?'var(--green)':d.side==='bear'?'var(--red)':'var(--text-dim)';const di=d.side==='bull'?'✓':d.side==='bear'?'✗':'·';return`<div style="display:flex;gap:5px;font-size:10px;color:${dc};padding:1px 0"><span style="width:10px">${di}</span><span>${d.label}</span></div>`;}).join('');
  panel.innerHTML=`<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px"><span style="font-size:15px;font-weight:600;color:${clr}">${htf.trend} ${icon}</span><span style="font-size:9px;color:var(--text-dim)">EMA${HTF_EMA_FAST}:${fmt(htf.ema_fast)} / EMA${HTF_EMA_SLOW}:${fmt(htf.ema_slow)}</span></div><div style="height:3px;background:var(--border);border-radius:2px;margin-bottom:6px;overflow:hidden"><div style="height:100%;width:${barW}%;background:${clr};border-radius:2px;transition:width .5s"></div></div><div>${dHtml}</div><div style="font-size:9px;color:var(--text-dim);margin-top:5px">RSI:${htf.rsi} · ${htf.ts||'—'} · ${htf.trend==='NEUTRAL'?'Her iki yön':'Yalnız '+htf.trend}</div>`;
}

function renderMkt(d){
  if(!d.mkt)return;const m=d.mkt;
  const ts=document.getElementById('mkt-ts');if(ts)ts.textContent=m.ts?`· ${m.ts}`:'';
  const fr=m.funding_rate||0,frP=(fr*100).toFixed(4),frSt=fr>0.0008?'fail':fr<-0.0008?'pass':'warn';
  setDecRow('fr',frSt,(fr>=0?'+':'')+frP+'% — '+(m.funding_str||''),Math.min(100,Math.abs(fr)/0.002*100),fr>0.0008?'var(--red)':fr<-0.0008?'var(--green)':'var(--amber)');
  const oi=m.oi_change_pct||0,oiSt=m.oi_trend==='artıyor'?'pass':m.oi_trend==='azalıyor'?'fail':'warn';
  setDecRow('oi',oiSt,(oi>=0?'+':'')+oi.toFixed(3)+'% — '+(m.oi_trend||'nötr'),Math.min(100,Math.abs(oi)/0.5*100),m.oi_trend==='artıyor'?'var(--green)':m.oi_trend==='azalıyor'?'var(--red)':'var(--amber)');
  const ls=m.ls_ratio||1,lsSt=ls>1.4?'fail':ls<0.7?'pass':'warn';
  setDecRow('ls',lsSt,ls.toFixed(2)+' — '+(m.ls_str||''),Math.min(100,ls/2*100),ls>1.4?'var(--red)':ls<0.7?'var(--green)':'var(--amber)');
  const tk=m.taker_ratio||1,tkSt=tk>1.25?'pass':tk<0.8?'fail':'warn';
  setDecRow('tk',tkSt,'×'+tk.toFixed(2)+' — '+(m.taker_str||''),Math.min(100,tk/2*100),tk>1.25?'var(--green)':tk<0.8?'var(--red)':'var(--amber)');
}

function renderFilterSummary(d){
  const bar=document.getElementById('filter-bar');if(!bar)return;
  const mkt=d.mkt||{},htf=d.htf||{},signals=d.signals||[];
  const active=signals.filter(s=>!s.htf_blocked),rsi=d.rsi||50;
  const items=[
    (()=>{const t=htf.trend||'—',clr=t==='BULL'?'#00d264':t==='BEAR'?'#ff3d5a':'#f0a500';return{label:'1h Trend',val:t,pct:(htf.strength||0)/4*100,clr};})(),
    (()=>{const clr=rsi>65?'#ff3d5a':rsi<35?'#00d264':'#f0a500';return{label:'RSI',val:rsi.toFixed(1),pct:rsi,clr};})(),
    (()=>{const fr=mkt.funding_rate||0,pct=Math.min(100,Math.abs(fr)/0.002*100),clr=fr>0.0008?'#ff3d5a':fr<-0.0008?'#00d264':'#f0a500';return{label:'Funding',val:(fr>=0?'+':'')+(fr*100).toFixed(4)+'%',pct,clr};})(),
    (()=>{const chg=mkt.oi_change_pct||0,pct=Math.min(100,Math.abs(chg)/0.5*100),clr=mkt.oi_trend==='artıyor'?'#00d264':mkt.oi_trend==='azalıyor'?'#ff3d5a':'#f0a500';return{label:'OI',val:(chg>=0?'+':'')+chg.toFixed(3)+'%',pct,clr};})(),
    (()=>{const ls=mkt.ls_ratio||1,pct=Math.min(100,ls/2*100),clr=ls>1.4?'#ff3d5a':ls<0.7?'#00d264':'#f0a500';return{label:'L/S',val:ls.toFixed(2),pct,clr};})(),
    (()=>{const tk=mkt.taker_ratio||1,pct=Math.min(100,tk/2*100),clr=tk>1.25?'#00d264':tk<0.8?'#ff3d5a':'#f0a500';return{label:'Taker',val:'×'+tk.toFixed(2),pct,clr};})(),
    (()=>{const n=active.length,clr=n>0?'#00d264':'#4a6070',score=n>0?active[0].score:0;return{label:'Sinyal',val:n>0?`${active[0].dir} ★${score}`:'—',pct:n>0?score/8*100:0,clr};})(),
  ];
  bar.innerHTML=items.map(it=>`<div class="fb-item"><span class="fb-label">${it.label}</span><div class="fb-bar-bg"><div class="fb-bar-fill" style="width:${it.pct.toFixed(1)}%;background:${it.clr}"></div></div><span class="fb-val" style="color:${it.clr}">${it.val}</span></div>`).join('');
}

function renderWinRate(d){
  const st=d.stats||{};
  const pctEl=document.getElementById('wr-pct'),metaEl=document.getElementById('wr-meta'),barEl=document.getElementById('wr-bar');
  if(!pctEl)return;
  if(!st.total){pctEl.textContent='—';pctEl.style.color='var(--text-dim)';metaEl.textContent='Sinyal bekleniyor';barEl.style.width='0%';}
  else{pctEl.textContent=st.win_rate+'%';const clr=st.win_rate>=55?'var(--green)':st.win_rate>=45?'var(--amber)':'var(--red)';pctEl.style.color=clr;const s=st.net_pnl_pct>=0?'+':'';metaEl.innerHTML=`${st.wins}K ${st.losses}L &nbsp;|&nbsp; <span style="color:${st.net_pnl_pct>=0?'var(--green)':'var(--red)'}">${s}${st.net_pnl_pct}%</span>`;barEl.style.width=st.win_rate+'%';barEl.style.background=clr;}
  const commInfo=document.getElementById('wr-comm-info');if(commInfo)commInfo.textContent=`Komisyon: %${(COMMISSION*100).toFixed(2)}×2 = %${st.comm_pct||0} (r/t)`;
  const lrEl=document.getElementById('wr-long-rate');if(lrEl){lrEl.textContent=st.long_total>0?st.long_rate+'%':'—';lrEl.style.color=st.long_rate>=55?'var(--green)':st.long_rate>=45?'var(--amber)':'var(--red)';}
  const lm=document.getElementById('wr-long-meta');if(lm)lm.textContent=`${st.long_wins||0}W/${(st.long_total-st.long_wins)||0}L (${st.long_total||0})`;
  const srEl=document.getElementById('wr-short-rate');if(srEl){srEl.textContent=st.short_total>0?st.short_rate+'%':'—';srEl.style.color=st.short_rate>=55?'var(--green)':st.short_rate>=45?'var(--amber)':'var(--red)';}
  const sm=document.getElementById('wr-short-meta');if(sm)sm.textContent=`${st.short_wins||0}W/${(st.short_total-st.short_wins)||0}L (${st.short_total||0})`;
}

function renderSignals(d){
  const area=document.getElementById('signal-area');
  if(!d.signals||!d.signals.length){area.innerHTML='<div class="no-signal">⏳ Sinyal yok</div>';return;}

  // Zaten pending'de takip edilen yönleri çıkar — duplikasyon önleme
  const pendingDirs = new Set((d.pending||[]).map(p=>p.dir));

  const active  = (d.signals||[]).filter(s => !s.htf_blocked && !pendingDirs.has(s.dir) && !s.already_tracked);
  const blocked = (d.signals||[]).filter(s =>  s.htf_blocked);

  // Aktif sinyal yok ama pending var — sessizce göster
  if (!active.length && !blocked.length) {
    area.innerHTML='<div class="no-signal" style="border-color:var(--green-dim);color:var(--text-dim)">✓ Sinyal takipte — Sonuç Bekleniyor bölümüne bak</div>';
    return;
  }

  let html='';
  if(!active.length&&blocked.length)html+=`<div class="no-signal" style="border-color:var(--amber-dim)"><div style="font-size:13px;margin-bottom:3px">🚫 HTF Filtresi</div><div style="font-size:10px">1h ${(d.htf&&d.htf.trend)||'?'} — ${blocked.length} sinyal engellendi</div></div>`;
  html+=active.map(s=>{const isLong=s.dir==='LONG',clr=isLong?'var(--green)':'var(--red)';
    return`<div class="signal-box ${isLong?'long':'short'}">
      <div class="sig-header"><span class="sig-dir" style="color:${clr}">${isLong?'🟢 LONG':'🔴 SHORT'}</span><span class="sig-score">${stars(s.score)} Skor ${s.score}</span></div>
      <div class="sig-levels">
        <div class="sig-level"><div class="sig-level-label">Giriş</div><div>$${fmt(s.entry)}</div></div>
        <div class="sig-level"><div class="sig-level-label">TP</div><div style="color:var(--green)">$${fmt(s.tp)}</div></div>
        <div class="sig-level"><div class="sig-level-label">SL</div><div style="color:var(--red)">$${fmt(s.sl)}</div></div>
      </div>
      <div style="font-size:9px;color:var(--text-dim);margin-bottom:4px">Net TP: <span style="color:var(--green)">+${s.net_tp_pct||0}%</span> · Net SL: <span style="color:var(--red)">-${s.net_sl_pct||0}%</span> · R/R: ${((s.net_tp_pct||0)/(s.net_sl_pct||1)).toFixed(2)}:1</div>
      <div class="checks">${(s.checks||[]).map(checkHtml).join('')}</div>
    </div>`;}).join('');
  if(blocked.length){html+=`<div style="font-size:9px;color:var(--text-dim);margin:8px 0 4px;letter-spacing:.1em;text-transform:uppercase">Engellendi (${blocked.length})</div>`;
    html+=blocked.map(s=>{const isLong=s.dir==='LONG',clr=isLong?'var(--green)':'var(--red)';return`<div class="signal-box" style="opacity:.3;border-left-color:var(--text-dim)"><div class="sig-header"><span style="color:${clr};text-decoration:line-through;font-size:12px">${s.dir}</span><span style="font-size:10px;color:var(--amber)">🚫 ${s.htf_trend}</span></div></div>`;}).join('');}
  area.innerHTML=html;
}

function renderPending(d){
  const area=document.getElementById('pending-area');
  document.getElementById('pending-count').textContent=d.pending.length?`(${d.pending.length})`:'';
  if(!d.pending.length){area.innerHTML='<div style="color:var(--text-dim);font-size:10px;padding:5px 0">Bekleyen sinyal yok</div>';return;}
  const curPrice = d.price || 0;
  area.innerHTML=d.pending.map(s=>{
    const isLong=s.dir==='LONG', clr=isLong?'var(--green)':'var(--red)';
    // Anlık kâr/zarar
    let livePct = 0, liveClr = 'var(--text-dim)';
    if(curPrice && s.entry){
      const rt = COMMISSION * 2;
      const raw = (curPrice - s.entry) / s.entry * (isLong ? 1 : -1);
      livePct = (raw - rt) * 100;
      liveClr = livePct > 0 ? 'var(--green)' : livePct < 0 ? 'var(--red)' : 'var(--text-dim)';
    }
    const pct2sl = Math.abs((s.sl - s.entry) / s.entry * 100).toFixed(2);
    const pct2tp = Math.abs((s.tp - s.entry) / s.entry * 100).toFixed(2);
    return`<div style="background:var(--bg3);border-radius:4px;padding:6px 9px;margin-bottom:4px;border-left:2px solid ${clr}">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
        <span class="badge pending">${s.dir}</span>
        <span style="color:${clr};font-size:11px;font-weight:600">$${fmt(s.entry)}</span>
        <span style="font-size:10px;color:${liveClr};margin-left:auto">
          ${livePct>=0?'+':''}${livePct.toFixed(2)}% <span style="font-size:9px;color:var(--text-dim)">şu an</span>
        </span>
      </div>
      <div style="display:flex;gap:10px;font-size:9px">
        <span>TP <span style="color:var(--green)">$${fmt(s.tp)}</span> <span style="color:var(--text-dim)">(+${pct2tp}%)</span></span>
        <span>SL <span style="color:var(--red)">$${fmt(s.sl)}</span> <span style="color:var(--text-dim)">(-${pct2sl}%)</span></span>
        <span style="margin-left:auto;color:var(--text-dim)">${s.ts||''}</span>
      </div>
    </div>`;
  }).join('');
}

function renderClosed(d){
  const area=document.getElementById('closed-area');
  if(!d.closed||!d.closed.length){area.innerHTML='<div style="color:var(--text-dim);font-size:10px;padding:5px 0">Henüz kapanmadı</div>';return;}
  area.innerHTML=d.closed.map(s=>{
    const isLong=(s.dir||s.direction)==='LONG';
    const isWin=s.outcome==='WIN';
    const isTimeout=(s.close_ts||'').includes('timeout');
    const pnlPct=s.net_pnl_pct||0, pnlSign=pnlPct>=0?'+':'';
    const pnlClr=isWin?'var(--green)':'var(--red)';
    const dur=s.duration_min;
    const durStr=dur!=null?(dur<60?dur+'dk':(dur/60).toFixed(1)+'sa'):'—';
    const openTs=(s.open_ts||s.ts||'').substring(0,8);
    const closeTs=(s.close_ts||'').substring(0,8);
    const exitP=s.exit_price;
    return`<div style="background:var(--bg3);border-radius:4px;padding:7px 10px;margin-bottom:5px;border-left:3px solid ${pnlClr}">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
        <span class="badge ${isWin?'win':'loss'}">${isTimeout?'⏱ ':''}${isWin?'WIN':'LOSS'}</span>
        <span style="color:${isLong?'var(--green)':'var(--red)'};font-size:11px;font-weight:600">${isLong?'LONG':'SHORT'}</span>
        <span style="font-size:11px;font-weight:600;color:${pnlClr}">${pnlSign}${pnlPct.toFixed(2)}%</span>
        <span style="font-size:9px;color:var(--text-dim);margin-left:auto">${durStr} · ${stars(s.score||0)}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:3px;font-size:9px;margin-bottom:3px">
        <div><div style="color:var(--text-dim)">Giriş</div><div style="font-weight:500">$$${fmt(s.entry||0)}</div></div>
        <div><div style="color:var(--text-dim)">Çıkış</div><div style="color:${pnlClr};font-weight:500">${exitP>0?'$$'+fmt(exitP):'—'}</div></div>
        <div><div style="color:var(--text-dim)">TP</div><div style="color:var(--green)">$$${fmt(s.tp||0)}</div></div>
        <div><div style="color:var(--text-dim)">SL</div><div style="color:var(--red)">$$${fmt(s.sl||0)}</div></div>
      </div>
      <div style="font-size:9px;color:${pnlClr};margin-bottom:3px">
        ${s.close_reason || (isWin ? 'TP hedefine ulaştı ✓' : 'SL tetiklendi ✗')}
      </div>
      <div style="font-size:9px;color:var(--text-dim);display:flex;gap:8px">
        <span>📅 ${openTs}</span><span>→ ${closeTs}</span>
        <span style="margin-left:auto">${durStr}</span>
      </div>
    </div>`;
  }).join('');
}

function renderNews(d){
  const list=document.getElementById('news-list');if(!list)return;
  if(!d.news){list.innerHTML='<div style="color:var(--text-dim);font-size:10px;text-align:center;padding:14px 0">Yükleniyor…</div>';return;}
  if(!d.news.length){list.innerHTML='<div style="color:var(--text-dim);font-size:10px;text-align:center;padding:14px 0">⚠ RSS erişilemedi</div>';return;}
  list.innerHTML=d.news.map(n=>{const srcClr={'CoinDesk':'#f0a500','CoinTelegraph':'#00c8e0','Decrypt':'#c070ff','Google News':'#00d264'}[n.source]||'var(--text-dim)';return`<div class="news-item" onclick="openLink('${encodeURI(n.url||'')}')"><div class="news-source" style="color:${srcClr}">${escHtml(n.source)}</div><div class="news-title">${escHtml(n.title)}</div><div class="news-time">${escHtml(n.ts||'')}</div></div>`;}).join('');
}

function renderTweets(d){
  const list=document.getElementById('tweet-list'),tags=document.getElementById('kw-tags');if(!list)return;
  const stLabel=document.getElementById('st-sym-label');
  if(stLabel&&d.symbol){const sym=d.symbol.split('/')[0],stSym={'BTC':'BTC.X','ETH':'ETH.X','SOL':'SOL.X','BNB':'BNB.X','XRP':'XRP.X','DOGE':'DOGE.X'}[sym]||sym+'.X';stLabel.textContent=stSym+' · 60sn';}
  if(tags)tags.innerHTML=Array.isArray(d.tweet_kw)&&d.tweet_kw.length?d.tweet_kw.map(k=>`<span class="kw-tag" onclick="removeKw('${escHtml(k)}')">${escHtml(k)} <span style="opacity:.5;font-size:9px">✕</span></span>`).join(''):'';
  if(!d.tweets||!d.tweets.length){list.innerHTML='<div style="color:var(--text-dim);font-size:10px;text-align:center;padding:14px 0">StockTwits bekleniyor…</div>';return;}
  list.innerHTML=d.tweets.map(t=>`<div class="tweet-item"><div class="tweet-user">${escHtml(t.user||'')}</div><div class="tweet-text">${escHtml(t.text)}</div><div class="tweet-time"><span>${escHtml(t.ts||'')}</span>${t.url?`<a href="${encodeURI(t.url)}" target="_blank" rel="noopener" style="color:var(--text-dim);text-decoration:none;margin-left:auto">→</a>`:''}</div></div>`).join('');
}

function changeSymbol(sym){fetch('/change_symbol',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});}
function setKeywords(){const raw=document.getElementById('kw-input').value.trim();if(!raw)return;fetch('/set_keywords',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keywords:raw})}).then(()=>document.getElementById('kw-input').value='');}
function removeKw(kw){const tags=document.getElementById('kw-tags');const current=Array.from(tags.querySelectorAll('.kw-tag')).map(el=>el.textContent.replace('✕','').trim()).filter(k=>k!==kw);fetch('/set_keywords',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keywords:current.join(', ')})});}
function refreshNews(){fetch('/refresh_news',{method:'POST'});}

function clearSignals(mode){
  const labels = {pending:'bekleyen sinyaller', closed:'sinyal geçmişi', all:'tüm sinyaller'};
  if(!confirm(`${labels[mode] || mode} silinecek. Emin misiniz?`)) return;
  fetch('/clear_signals',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode, symbol: window._lastSymbol || undefined})
  }).then(r=>r.json()).then(d=>{
    console.log('[SİL]', d);
  });
}

// ── SSE ──────────────────────────────────────────────────
const src=new EventSource('/stream');
src.onmessage=e=>{
  let d;try{d=JSON.parse(e.data);}catch(err){console.error('SSE',err);return;}
  if(d.symbol){window._lastSymbol=d.symbol;const sel=document.getElementById('symbol-select');if(sel&&sel.value!==d.symbol)sel.value=d.symbol;const lbl=document.getElementById('h-symbol-label');if(lbl){const p=d.symbol.split('/');lbl.innerHTML=p[0]+'<span>/'+( p[1]||'USDT')+'</span> · SİNYAL BOTU';}}
  try{renderHeader(d);}catch(e){console.error('header',e);}
  try{renderIndicators(d);}catch(e){console.error('indicators',e);}
  try{renderOrderBook(d);}catch(e){console.error('orderbook',e);}
  try{renderTech(d);}catch(e){console.error('tech',e);}
  try{renderHTF(d);}catch(e){console.error('htf',e);}
  try{renderMkt(d);}catch(e){console.error('mkt',e);}
  try{renderFilterSummary(d);}catch(e){console.error('filterbar',e);}
  try{renderNews(d);}catch(e){console.error('news',e);}
  try{renderTweets(d);}catch(e){console.error('tweets',e);}
  try{renderWinRate(d);}catch(e){console.error('winrate',e);}
  try{renderSignals(d);}catch(e){console.error('signals',e);}
  try{renderPending(d);}catch(e){console.error('pending',e);}
  try{renderClosed(d);}catch(e){console.error('closed',e);}
  try{if(d.candles&&d.candles.length)initChart(d.candles);}catch(e){console.error('chart',e);}
  const dot=document.getElementById('dot');if(dot){dot.style.background='var(--green)';dot.style.boxShadow='0 0 6px var(--green)';}
};
src.onerror=()=>{const dot=document.getElementById('dot');if(dot){dot.style.background='var(--red)';dot.style.boxShadow='0 0 6px var(--red)';}};
</script>
</body>
</html>"""

# ── Flask Routes ──────────────────────────────────────────────
@app.route("/")
def index():
    html=HTML.replace("__PROXIMITY__",str(PROXIMITY_PCT)).replace("__TP__",str(TP_PCT)).replace("__SL__",str(SL_PCT)).replace("__COMM__",str(COMMISSION)).replace("__HTF_EMA_FAST__",str(HTF_EMA_FAST)).replace("__HTF_EMA_SLOW__",str(HTF_EMA_SLOW)).replace("__EMA_FAST__",str(EMA_FAST)).replace("__EMA_SLOW__",str(EMA_SLOW))
    return render_template_string(html)

@app.route("/stream")
def stream():
    def event_stream():
        last_ts=None
        while True:
            with _lock: ts=_state.get("ts"); state=dict(_state)
            if ts and ts!=last_ts: yield f"data: {json.dumps(state)}\n\n"; last_ts=ts
            time.sleep(1)
    return Response(event_stream(),mimetype="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/set_keywords",methods=["POST"])
def set_keywords():
    global _tweet_keywords,_tweet_last_fetch
    data=flask_request.get_json(silent=True) or {}
    kws=[k.strip() for k in re.split(r"[,\s]+",data.get("keywords","")) if k.strip()][:6]
    _tweet_keywords=kws; _tweet_last_fetch=0
    return {"ok":True,"keywords":kws}

@app.route("/change_symbol",methods=["POST"])
def change_symbol():
    global SYMBOL,_tweet_keywords,_tweet_last_fetch,_news_last_fetch
    data=flask_request.get_json(silent=True) or {}
    sym=data.get("symbol","").strip().upper()
    if "/" not in sym: sym=sym+"/USDT"
    SYMBOL=sym; _tweet_keywords=[sym.split("/")[0]]; _tweet_last_fetch=0; _news_last_fetch=0
    return {"ok":True,"symbol":SYMBOL}

@app.route("/refresh_news",methods=["POST"])
def refresh_news():
    global _news_last_fetch; _news_last_fetch=0; return {"ok":True}

@app.route("/history")
def history():
    """Son 200 kapanmış sinyali JSON olarak döndür."""
    symbol = flask_request.args.get("symbol", SYMBOL)
    rows   = db_load_closed(symbol=symbol, limit=200)
    return json.dumps(rows)

@app.route("/db/stats")
def db_stats_endpoint():
    """Tüm zamanların istatistiği (tüm symbol'ler)."""
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT symbol,
                   COUNT(*)                                    AS total,
                   SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                   ROUND(AVG(CASE WHEN outcome='WIN' THEN 100.0 ELSE 0 END),1) AS win_rate,
                   ROUND(SUM(COALESCE(net_pnl_pct,0)),2)      AS net_pnl_pct,
                   MIN(open_ts)                                AS first_signal,
                   MAX(open_ts)                                AS last_signal
            FROM signals WHERE status != 'pending'
            GROUP BY symbol ORDER BY total DESC
        """).fetchall()
    return json.dumps([dict(r) for r in rows])

@app.route("/clear_signals", methods=["POST"])
def clear_signals():
    """Sinyalleri sil. ?mode=all | pending | closed | symbol=BTC/USDT"""
    global _pending_signals, _closed_signals
    data   = flask_request.get_json(silent=True) or {}
    mode   = data.get("mode", "all")          # all / pending / closed
    symbol = data.get("symbol")               # None = tüm symboller

    with db_connect() as conn:
        if mode in ("all", "pending"):
            q = "DELETE FROM signals WHERE status='pending'"
            params = ()
            if symbol:
                q += " AND symbol=?"; params = (symbol,)
            conn.execute(q, params)
            _pending_signals = [s for s in _pending_signals
                                if symbol and s.get("symbol") != symbol] if symbol else []

        if mode in ("all", "closed"):
            q = "DELETE FROM signals WHERE status!='pending'"
            params = ()
            if symbol:
                q += " AND symbol=?"; params = (symbol,)
            conn.execute(q, params)
            _closed_signals = [s for s in _closed_signals
                               if symbol and s.get("symbol") != symbol] if symbol else []
        conn.commit()

    with db_connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    print(f"[SİL] mode={mode} symbol={symbol or 'tümü'} → {remaining} kayıt kaldı")
    return {"ok": True, "remaining": remaining}

if __name__=="__main__":
    load_signals()
    threading.Thread(target=background_loop,daemon=True).start()
    print("\n✅  Dashboard hazır → http://localhost:5000\n")
    app.run(debug=False,port=5000,threaded=True)
