"""
BTC/USDT Sinyal Botu — AlwaysData Versiyonu
WSGI uyumlu, veritabanı ile çalışır
"""

import json, time, threading, requests, re, html as html_lib, os, sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, Response, render_template_string, request as flask_request
import ccxt, pandas as pd, ta

SYMBOL = "BTC/USDT"
AVAILABLE_SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT","DOGE/USDT"]
TIMEFRAME = "5m"
CANDLE_LIMIT = 100
HTF_TIMEFRAME = "1h"
HTF_LIMIT = 60
HTF_EMA_FAST = 20
HTF_EMA_SLOW = 50
HTF_RSI_OB = 60
HTF_RSI_OS = 40
HTF_REFRESH = 60
BNFUT_BASE = "https://fapi.binance.com"
MKT_REFRESH = 30
FUND_STRONG = 0.0008
FUND_WEAK = -0.0008
LS_CROWD_LONG = 1.4
LS_CROWD_SHORT = 0.7
TAKER_STRONG = 1.25
OI_CHANGE_THR = 0.005
OB_DEPTH = 100
TOP_WALLS = 6
BUCKET_PCT = 0.0015
PROXIMITY_PCT = 0.005
MIN_WALL_BTC = 4.0
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OB = 65
RSI_OS = 35
VOL_MULTIPLIER = 1.8
TP_PCT = 0.02
SL_PCT = 0.01
COMMISSION = 0.0015
MIN_SCORE = 2
REFRESH_SEC = 15

CONF_WEAK = 40
CONF_MODERATE = 60
CONF_STRONG = 75
CONF_VSTRONG = 88
W_TREND = 30
W_MOMENTUM = 25
W_STRUCTURE = 20
W_MARKET = 25

TWEET_REFRESH = 60
NEWS_REFRESH = 120
NEWS_MAX = 40
TWEET_MAX = 30

NEWS_FEEDS = [
    {"name": "Google News", "url": "", "dynamic": True},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed"},
]

STOCKTWITS_MAP = {"BTC":"BTC.X","ETH":"ETH.X","SOL":"SOL.X","BNB":"BNB.X","XRP":"XRP.X","DOGE":"DOGE.X"}
REDDIT_SUBS = ["Bitcoin","CryptoCurrency","btc","ethereum","CryptoMarkets"]
REDDIT_HEADERS = {"User-Agent": "btc-dashboard/1.0", "Accept": "application/json"}

app = Flask(__name__)
exchange = ccxt.binance({"options": {"defaultType": "future"}})

# SQLite setup
import queue as _queue_mod
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db")
_db_queue = _queue_mod.Queue()
_db_rconn = None
_db_rlock = threading.Lock()

def _get_rconn():
    global _db_rconn
    with _db_rlock:
        if _db_rconn is None:
            _db_rconn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, check_same_thread=False)
            _db_rconn.row_factory = sqlite3.Row
        return _db_rconn

def _db_writer_loop():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.commit()
    while True:
        try:
            fn, args, result_q = _db_queue.get(timeout=1)
            try:
                result = fn(conn, *args)
                if result_q is not None:
                    result_q.put(("ok", result))
            except Exception as e:
                print(f"[DB WRITE ERR] {e}")
                if result_q is not None:
                    result_q.put(("err", e))
            finally:
                _db_queue.task_done()
        except _queue_mod.Empty:
            continue

def _db_write(fn, *args, wait=True):
    rq = _queue_mod.Queue() if wait else None
    _db_queue.put((fn, args, rq))
    if wait and rq is not None:
        status, result = rq.get()
        if status == "err":
            raise result
        return result
    return None

def _db_read(sql, params=()):
    try:
        conn = _get_rconn()
        with _db_rlock:
            rows = conn.execute(sql, params).fetchall()
        return rows
    except Exception:
        def _fn(conn, s, p): return conn.execute(s, p).fetchall()
        return _db_write(_fn, sql, params)

_db_writer_thread = threading.Thread(target=_db_writer_loop, daemon=True)
_db_writer_thread.start()

def db_init():
    def _fn(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                tp REAL NOT NULL,
                sl REAL NOT NULL,
                score INTEGER NOT NULL,
                net_tp_pct REAL,
                net_sl_pct REAL,
                wall_price REAL,
                wall_vol REAL,
                htf_trend TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                outcome TEXT,
                exit_price REAL,
                net_pnl_pct REAL,
                net_pnl_usd REAL,
                duration_min INTEGER,
                close_reason TEXT,
                open_ts TEXT NOT NULL,
                close_ts TEXT,
                checks_json TEXT
            )""")
        conn.commit()
        for col, typ in [('exit_price','REAL'),('duration_min','INTEGER'),('close_reason','TEXT')]:
            try:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
                conn.commit()
            except Exception:
                pass
        return True
    _db_write(_fn)

def db_insert_signal(sig):
    def _fn(conn, s):
        cur = conn.execute("""
            INSERT INTO signals (symbol,direction,entry,tp,sl,score,net_tp_pct,net_sl_pct,wall_price,wall_vol,htf_trend,status,open_ts,checks_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
        """, (s.get("symbol",SYMBOL), s["dir"], s["entry"], s["tp"], s["sl"], s["score"],
               s.get("net_tp_pct"), s.get("net_sl_pct"), s.get("wall_price"), s.get("wall_vol"),
               s.get("htf_trend"), s.get("ts",datetime.now().strftime("%H:%M:%S")),
               json.dumps([c["label"] for c in s.get("checks",[])])))
        conn.commit()
        return cur.lastrowid
    return _db_write(_fn, sig)

def db_close_signal(rowid, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price=None, duration_min=None, close_reason=None):
    def _fn(conn, rid, out, pct, usd, cts, ep, dm, cr):
        conn.execute("""
            UPDATE signals SET status=?,outcome=?,net_pnl_pct=?,net_pnl_usd=?,close_ts=?,exit_price=?,duration_min=?,close_reason=?
            WHERE id=?""", (out.lower(), out, pct, usd, cts, ep, dm, cr, rid))
        conn.commit()
    _db_write(_fn, rowid, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price, duration_min, close_reason, wait=False)

def db_load_pending():
    rows = _db_read("SELECT * FROM signals WHERE status='pending' ORDER BY id")
    result = []
    for r in rows:
        d = dict(r)
        d["dir"] = d.pop("direction")
        d["checks"] = [{"label":l,"status":"pass","side":""} for l in json.loads(d.pop("checks_json") or "[]")]
        d["_db_id"] = d["id"]
        d["ts"] = d.pop("open_ts")
        d["htf_blocked"] = False
        result.append(d)
    return result

def db_load_closed(symbol=None, limit=100):
    if symbol:
        rows = _db_read("SELECT * FROM signals WHERE status!='pending' AND symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
    else:
        rows = _db_read("SELECT * FROM signals WHERE status!='pending' ORDER BY id DESC LIMIT ?", (limit,))
    result = []
    for r in rows:
        d = dict(r)
        d["dir"] = d.pop("direction")
        d["checks"] = []
        d["close_ts"] = d.get("close_ts") or d.get("open_ts","")
        result.append(d)
    return result

def db_win_stats(symbol=None):
    if symbol:
        rows = _db_read("SELECT * FROM signals WHERE status!='pending' AND symbol=?", (symbol,))
    else:
        rows = _db_read("SELECT * FROM signals WHERE status!='pending'")
    closed = [dict(r) for r in rows]
    if not closed:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,"long_total":0,"long_wins":0,"long_rate":0,
                "short_total":0,"short_wins":0,"short_rate":0,"net_pnl_pct":0,"net_pnl_usd":0,"comm_pct":round(2*COMMISSION*100,2)}
    wins = sum(1 for s in closed if s["outcome"]=="WIN")
    longs = [s for s in closed if s["direction"]=="LONG"]
    shorts = [s for s in closed if s["direction"]=="SHORT"]
    lw = sum(1 for s in longs if s["outcome"]=="WIN")
    sw = sum(1 for s in shorts if s["outcome"]=="WIN")
    return {"total":len(closed),"wins":wins,"losses":len(closed)-wins,"win_rate":round(wins/len(closed)*100,1),
            "long_total":len(longs),"long_wins":lw,"long_rate":round(lw/len(longs)*100,1) if longs else 0,
            "short_total":len(shorts),"short_wins":sw,"short_rate":round(sw/len(shorts)*100,1) if shorts else 0,
            "net_pnl_pct":round(sum(s.get("net_pnl_pct") or 0 for s in closed),2),
            "net_pnl_usd":round(sum(s.get("net_pnl_usd") or 0 for s in closed),2),
            "comm_pct":round(2*COMMISSION*100,2)}

_lock = threading.Lock()
_state = {}
_pending_signals = []
_closed_signals = []

def load_signals():
    global _pending_signals, _closed_signals
    try:
        db_init()
        _pending_signals = db_load_pending()
        _closed_signals = db_load_closed(limit=100)
        for s in _pending_signals + _closed_signals:
            if "symbol" not in s: s["symbol"] = SYMBOL
        print(f"[DB] Loaded: {len(_pending_signals)} pending, {len(_closed_signals)} closed")
    except Exception as e:
        print(f"[DB ERROR] Load: {e}")
        _pending_signals = []; _closed_signals = []

_htf_cache = {"trend":"NEUTRAL","ema_fast":0,"ema_slow":0,"rsi":50,"score":0,"details":[],"ts":None,"strength":0,"bull_sc":0,"bear_sc":0}
_htf_last_fetch = 0
_mkt_cache = {"funding_rate":0.0,"funding_str":"waiting","oi_now":0.0,"oi_prev":0.0,"oi_change_pct":0.0,"oi_trend":"waiting",
              "ls_ratio":1.0,"ls_str":"waiting","taker_buy":0.0,"taker_sell":0.0,"taker_ratio":1.0,"taker_str":"waiting","ts":"—"}
_mkt_last_fetch = -999
_news_cache = []
_news_last_fetch = -999
_tweet_cache = []
_tweet_last_fetch = -999
_tweet_keywords = [SYMBOL.split("/")[0]]

def fetch_ohlcv():
    raw = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=CANDLE_LIMIT)
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    return df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})

def load_indicators(df):
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], EMA_FAST).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], EMA_SLOW).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], RSI_PERIOD).rsi()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["body"] = df["close"] - df["open"]
    df["body_size"] = df["body"].abs()
    df["wick_up"] = df["high"] - df[["open","close"]].max(axis=1)
    df["wick_down"] = df[["open","close"]].min(axis=1) - df["low"]
    return df

def fetch_htf_ohlcv():
    raw = exchange.fetch_ohlcv(SYMBOL, HTF_TIMEFRAME, limit=HTF_LIMIT)
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    return df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})

def calc_htf_trend(df_htf):
    df = df_htf.copy()
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], HTF_EMA_FAST).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], HTF_EMA_SLOW).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], 14).rsi()
    c=df.iloc[-1]; p2=df.iloc[-4]
    bull_score=0; bear_score=0; details=[]
    if c["ema_fast"]>c["ema_slow"]: bull_score+=1; details.append({"label":f"1h EMA{HTF_EMA_FAST}>EMA{HTF_EMA_SLOW}","side":"bull"})
    else: bear_score+=1; details.append({"label":f"1h EMA{HTF_EMA_FAST}<EMA{HTF_EMA_SLOW}","side":"bear"})
    slope=c["ema_slow"]-p2["ema_slow"]
    if slope>0: bull_score+=1; details.append({"label":f"1h EMA{HTF_EMA_SLOW} rising","side":"bull"})
    else: bear_score+=1; details.append({"label":f"1h EMA{HTF_EMA_SLOW} falling","side":"bear"})
    rsi=c["rsi"]
    if rsi>HTF_RSI_OB: bull_score+=1; details.append({"label":f"1h RSI bullish ({rsi:.0f})","side":"bull"})
    elif rsi<HTF_RSI_OS: bear_score+=1; details.append({"label":f"1h RSI bearish ({rsi:.0f})","side":"bear"})
    else: details.append({"label":f"1h RSI neutral ({rsi:.0f})","side":"neutral"})
    if c["close"]>c["ema_slow"]: bull_score+=1; details.append({"label":f"1h price above EMA{HTF_EMA_SLOW}","side":"bull"})
    else: bear_score+=1; details.append({"label":f"1h price below EMA{HTF_EMA_SLOW}","side":"bear"})
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
        fr_str=f"extreme LONG ({fr*100:.4f}%)" if fr>FUND_STRONG else f"extreme SHORT ({fr*100:.4f}%)" if fr<FUND_WEAK else f"light {'positive' if fr>0 else 'negative'} ({fr*100:.4f}%)"
        result["funding_rate"]=round(fr,6); result["funding_str"]=fr_str
    except Exception as e: print(f"[MKT/Funding] {e}")
    try:
        oi_now=float(_get("/fapi/v1/openInterest",{"symbol":sym})["openInterest"])
        history=_get("/futures/data/openInterestHist",{"symbol":sym,"period":"5m","limit":6})
        oi_prev=float(history[0]["sumOpenInterest"]) if history else oi_now
        oi_chg=(oi_now-oi_prev)/oi_prev if oi_prev>0 else 0
        oi_trend="neutral"
        if abs(oi_chg)>=OI_CHANGE_THR: oi_trend="rising" if oi_chg>0 else "falling"
        result.update({"oi_now":round(oi_now,0),"oi_prev":round(oi_prev,0),"oi_change_pct":round(oi_chg*100,3),"oi_trend":oi_trend})
    except Exception as e: print(f"[MKT/OI] {e}")
    try:
        ls_data=_get("/futures/data/globalLongShortAccountRatio",{"symbol":sym,"period":"5m","limit":1})
        ls=float(ls_data[0]["longShortRatio"]) if ls_data else 1.0
        ls_str=f"crowd LONG ({ls:.2f})" if ls>LS_CROWD_LONG else f"crowd SHORT ({ls:.2f})" if ls<LS_CROWD_SHORT else f"balanced ({ls:.2f})"
        result["ls_ratio"]=round(ls,3); result["ls_str"]=ls_str
    except Exception as e: print(f"[MKT/LS] {e}")
    try:
        klines=_get("/fapi/v1/klines",{"symbol":sym,"interval":"5m","limit":6})
        if klines:
            tv=sum(float(k[5]) for k in klines); tb=sum(float(k[9]) for k in klines); ts=tv-tb
            tk=tb/ts if ts>0 else 1.0
            tk_str=f"aggressive buyers ({tk:.2f})" if tk>TAKER_STRONG else f"aggressive sellers ({tk:.2f})" if tk<1/TAKER_STRONG else f"balanced ({tk:.2f})"
            result.update({"taker_buy":round(tb,2),"taker_sell":round(ts,2),"taker_ratio":round(tk,3),"taker_str":tk_str})
    except Exception as e: print(f"[MKT/Taker] {e}")
    result["ts"]=datetime.now().strftime("%H:%M:%S")
    return result

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
            r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=8); r.raise_for_status()
            root=ET.fromstring(r.content)
            for item in root.findall(".//item")[:10]:
                title=_strip_html(item.findtext("title",""))
                link=(item.findtext("link") or "").strip()
                pubdate=_parse_rss_time(item.findtext("pubDate",""))
                if title: items.append({"title":title,"source":feed["name"],"url":link,"ts":pubdate,"raw_ts":pubdate})
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
        print(f"[StockTwits] {len(items)} messages ({st_sym})")
    except Exception as e:
        print(f"[StockTwits] {e}")
    items.sort(key=lambda x:x.get("raw_ts",""),reverse=True)
    return items[:TWEET_MAX]

def cluster_walls(orders,ref,n):
    buckets={}
    for price,qty in orders:
        b=round(price/(ref*BUCKET_PCT))*(ref*BUCKET_PCT); buckets[b]=buckets.get(b,0)+qty
    buckets={p:v for p,v in buckets.items() if v>=MIN_WALL_BTC}
    top=sorted(buckets.items(),key=lambda x:x[1],reverse=True)[:n]
    return [(p,v) for p,v in top]

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

def calc_confluence(df, direction, htf, mkt):
    is_long = direction == "LONG"
    c = df.iloc[-1]; p = df.iloc[-2]
    checks = []; hard = False
    k1 = 0
    htf_trend = htf.get("trend", "NEUTRAL")
    if (is_long and htf_trend == "BULL") or (not is_long and htf_trend == "BEAR"):
        htf_pts = 18
        checks.append({"label": f"1h {htf_trend} ✓ ({htf.get('strength',0)}/4)","status": "pass","side": "long" if is_long else "short","layer": 1,"pts": htf_pts})
    elif htf_trend == "NEUTRAL":
        htf_pts = 8
        checks.append({"label": f"1h NEUTRAL ({htf.get('strength',0)}/4) — weak trend","status": "warn","side": "neutral","layer": 1,"pts": htf_pts})
    else:
        htf_pts = 0; hard = True
        checks.append({"label": f"🚫 1h {htf_trend} — {direction} blocked","status": "fail","side": "","layer": 1,"pts": 0})
    k1 += htf_pts
    htf_str = htf.get("strength", 0)
    if not hard:
        str_pts = 8 if htf_str >= 4 else 4 if htf_str >= 3 else 0
        if str_pts:
            checks.append({"label": f"1h trend strong ({htf_str}/4)","status": "pass","side": "neutral","layer": 1,"pts": str_pts})
        k1 += str_pts
    ema_aligned = (c["ema_fast"] > c["ema_slow"]) if is_long else (c["ema_fast"] < c["ema_slow"])
    ema_pts = 4 if ema_aligned else 0
    if ema_aligned:
        checks.append({"label": f"5m EMA{EMA_FAST}{'>' if is_long else '<'}EMA{EMA_SLOW}","status": "pass","side": "long" if is_long else "short","layer": 1,"pts": ema_pts})
    else:
        checks.append({"label": f"5m EMA against","status": "fail","side": "","layer": 1,"pts": 0})
    k1 += ema_pts
    k1 = min(k1, W_TREND)

    k2 = 0
    rsi = float(c["rsi"]) if not pd.isna(c["rsi"]) else 50.0
    if is_long:
        if rsi < 25: rsi_pts = 12; rsi_lbl = f"RSI oversold ({rsi:.0f}) 🔥"
        elif rsi < RSI_OS: rsi_pts = 8; rsi_lbl = f"RSI oversold ({rsi:.0f})"
        elif rsi < 50: rsi_pts = 3; rsi_lbl = f"RSI low ({rsi:.0f})"
        else: rsi_pts = 0; rsi_lbl = f"RSI high ({rsi:.0f}) — against LONG"
    else:
        if rsi > 75: rsi_pts = 12; rsi_lbl = f"RSI overbought ({rsi:.0f}) 🔥"
        elif rsi > RSI_OB: rsi_pts = 8; rsi_lbl = f"RSI overbought ({rsi:.0f})"
        elif rsi > 50: rsi_pts = 3; rsi_lbl = f"RSI high ({rsi:.0f})"
        else: rsi_pts = 0; rsi_lbl = f"RSI low ({rsi:.0f}) — against SHORT"
    checks.append({"label": rsi_lbl,"status": "pass" if rsi_pts >= 8 else "warn" if rsi_pts > 0 else "fail","side": "long" if is_long else "short","layer": 2,"pts": rsi_pts})
    k2 += rsi_pts
    div = False
    if is_long and len(df) >= 6:
        lp = df["low"].iloc[-6:]; lr = df["rsi"].iloc[-6:]
        if lp.iloc[-1] <= lp.min() and lr.iloc[-1] > lr.min(): div = True
    elif not is_long and len(df) >= 6:
        hp = df["high"].iloc[-6:]; hr = df["rsi"].iloc[-6:]
        if hp.iloc[-1] >= hp.max() and hr.iloc[-1] < hr.max(): div = True
    if div:
        checks.append({"label": f"RSI Divergence ✓","status": "pass","side": "long" if is_long else "short","layer": 2,"pts": 8})
        k2 += 8
    cross = ((p["ema_fast"] < p["ema_slow"]) and (c["ema_fast"] > c["ema_slow"])) if is_long else ((p["ema_fast"] > p["ema_slow"]) and (c["ema_fast"] < c["ema_slow"]))
    if cross:
        checks.append({"label": f"EMA{EMA_FAST}/{EMA_SLOW} crossover (fresh)","status": "pass","side": "long" if is_long else "short","layer": 2,"pts": 5})
        k2 += 5
    k2 = min(k2, W_MOMENTUM)

    k3 = 0
    vol_ma = float(c["vol_ma"]) if not pd.isna(c["vol_ma"]) and c["vol_ma"] > 0 else 1
    vr = float(c["volume"]) / vol_ma
    if vr >= 2.5: vol_pts = 10; vol_lbl = f"Volume strong spike ×{vr:.1f} 🔥"
    elif vr >= VOL_MULTIPLIER: vol_pts = 6; vol_lbl = f"Volume spike ×{vr:.1f}"
    elif vr >= 1.3: vol_pts = 3; vol_lbl = f"Volume rising ×{vr:.1f}"
    else: vol_pts = 0; vol_lbl = f"Volume weak ×{vr:.1f}"
    checks.append({"label": vol_lbl,"status": "pass" if vol_pts >= 6 else "warn" if vol_pts > 0 else "fail","side": "neutral","layer": 3,"pts": vol_pts})
    k3 += vol_pts
    pat = detect_candle(df, is_long)
    if pat:
        checks.append({"label": f"{pat}","status": "pass","side": "long" if is_long else "short","layer": 3,"pts": 10})
        k3 += 10
    else:
        checks.append({"label": "No clear pattern","status": "fail","side": "","layer": 3,"pts": 0})
    k3 = min(k3, W_STRUCTURE)

    k4 = 0
    fr = mkt.get("funding_rate", 0)
    if is_long:
        if fr > FUND_STRONG:
            hard = True
            checks.append({"label": f"🚫 Funding too high ({fr*100:.4f}%) — crowd LONG","status": "fail","side": "short","layer": 4,"pts": 0})
        elif fr < FUND_WEAK:
            fr_pts = 7
            checks.append({"label": f"Funding negative ({fr*100:.4f}%) — contrarian LONG ✓","status": "pass","side": "long","layer": 4,"pts": fr_pts})
            k4 += fr_pts
        elif fr < 0:
            fr_pts = 3
            checks.append({"label": f"Funding light negative ({fr*100:.4f}%)","status": "warn","side": "neutral","layer": 4,"pts": fr_pts})
            k4 += fr_pts
        else:
            checks.append({"label": f"Funding neutral ({fr*100:.4f}%)","status": "warn","side": "neutral","layer": 4,"pts": 0})
    else:
        if fr < FUND_WEAK:
            hard = True
            checks.append({"label": f"🚫 Funding too negative ({fr*100:.4f}%) — crowd SHORT","status": "fail","side": "long","layer": 4,"pts": 0})
        elif fr > FUND_STRONG:
            fr_pts = 7
            checks.append({"label": f"Funding positive ({fr*100:.4f}%) — contrarian SHORT ✓","status": "pass","side": "short","layer": 4,"pts": fr_pts})
            k4 += fr_pts
        elif fr > 0:
            fr_pts = 3
            checks.append({"label": f"Funding light positive ({fr*100:.4f}%)","status": "warn","side": "neutral","layer": 4,"pts": fr_pts})
            k4 += fr_pts
        else:
            checks.append({"label": f"Funding neutral ({fr*100:.4f}%)","status": "warn","side": "neutral","layer": 4,"pts": 0})

    oi_chg = mkt.get("oi_change_pct", 0)
    oi_trend = mkt.get("oi_trend", "neutral")
    if oi_trend == "falling":
        hard = True
        checks.append({"label": f"🚫 OI falling ({oi_chg:.3f}%) — positions closing","status": "fail","side": "","layer": 4,"pts": 0})
    elif oi_trend == "rising":
        oi_pts = 7 if abs(oi_chg) > OI_CHANGE_THR * 2 else 5
        checks.append({"label": f"OI rising +{oi_chg:.3f}% — new money ✓","status": "pass","side": "long" if is_long else "short","layer": 4,"pts": oi_pts})
        k4 += oi_pts
    else:
        checks.append({"label": f"OI neutral ({oi_chg:+.3f}%)","status": "warn","side": "neutral","layer": 4,"pts": 0})

    ls = mkt.get("ls_ratio", 1.0)
    if is_long:
        if ls > LS_CROWD_LONG:
            hard = True
            checks.append({"label": f"🚫 Crowd LONG ({ls:.2f}) — contrarian risk","status": "fail","side": "short","layer": 4,"pts": 0})
        elif ls < LS_CROWD_SHORT:
            ls_pts = 6
            checks.append({"label": f"Crowd SHORT ({ls:.2f}) — contrarian LONG ✓","status": "pass","side": "long","layer": 4,"pts": ls_pts})
            k4 += ls_pts
        else:
            checks.append({"label": f"L/S balanced ({ls:.2f})","status": "warn","side": "neutral","layer": 4,"pts": 0})
    else:
        if ls < LS_CROWD_SHORT:
            hard = True
            checks.append({"label": f"🚫 Crowd SHORT ({ls:.2f}) — contrarian risk","status": "fail","side": "long","layer": 4,"pts": 0})
        elif ls > LS_CROWD_LONG:
            ls_pts = 6
            checks.append({"label": f"Crowd LONG ({ls:.2f}) — contrarian SHORT ✓","status": "pass","side": "short","layer": 4,"pts": ls_pts})
            k4 += ls_pts
        else:
            checks.append({"label": f"L/S balanced ({ls:.2f})","status": "warn","side": "neutral","layer": 4,"pts": 0})

    tk = mkt.get("taker_ratio", 1.0)
    if is_long:
        if tk < 1 / TAKER_STRONG:
            hard = True
            checks.append({"label": f"🚫 Aggressive sellers (×{tk:.2f}) — against LONG","status": "fail","side": "short","layer": 4,"pts": 0})
        elif tk > TAKER_STRONG:
            tk_pts = 5
            checks.append({"label": f"Aggressive buyers (×{tk:.2f}) ✓","status": "pass","side": "long","layer": 4,"pts": tk_pts})
            k4 += tk_pts
        else:
            checks.append({"label": f"Taker balanced (×{tk:.2f})","status": "warn","side": "neutral","layer": 4,"pts": 0})
    else:
        if tk > TAKER_STRONG:
            hard = True
            checks.append({"label": f"🚫 Aggressive buyers (×{tk:.2f}) — against SHORT","status": "fail","side": "long","layer": 4,"pts": 0})
        elif tk < 1 / TAKER_STRONG:
            tk_pts = 5
            checks.append({"label": f"Aggressive sellers (×{tk:.2f}) ✓","status": "pass","side": "short","layer": 4,"pts": tk_pts})
        else:
            checks.append({"label": f"Taker balanced (×{tk:.2f})","status": "warn","side": "neutral","layer": 4,"pts": 0})
    k4 = min(k4, W_MARKET)

    total = k1 + k2 + k3 + k4
    if total >= CONF_VSTRONG: grade = "VERY STRONG"; stars = 4
    elif total >= CONF_STRONG: grade = "STRONG"; stars = 3
    elif total >= CONF_MODERATE: grade = "MODERATE"; stars = 2
    elif total >= CONF_WEAK: grade = "WEAK"; stars = 1
    else: grade = "INSUFFICIENT"; stars = 0

    return {"total":total,"grade":grade,"stars":stars,"hard":hard,"checks":checks,"k1":k1,"k2":k2,"k3":k3,"k4":k4,"mkt_score":k4}

def generate_signals(price, bid_walls, ask_walls, df):
    htf = _htf_cache
    candidates = []
    def _build(direction, wp, wv, dist):
        conf = calc_confluence(df, direction, htf, _mkt_cache)
        is_long = direction == "LONG"
        tp = round(price * (1 + TP_PCT if is_long else 1 - TP_PCT), 2)
        sl = round(price * (1 - SL_PCT if is_long else 1 + SL_PCT), 2)
        rt = 2 * COMMISSION
        return {"dir":direction,"entry":price,"tp":tp,"sl":sl,"net_tp_pct":round((TP_PCT - rt)*100, 2),"net_sl_pct":round((SL_PCT + rt)*100, 2),
                "net_tp_usd":round(price*(TP_PCT - rt), 2),"net_sl_usd":round(price*(SL_PCT + rt), 2),"comm_usd":round(price*rt, 2),
                "wall_price":wp,"wall_vol":round(wv, 2),"dist_pct":round(dist*100, 3),"score":conf["stars"],"conf_total":conf["total"],
                "conf_grade":conf["grade"],"conf_k1":conf["k1"],"conf_k2":conf["k2"],"conf_k3":conf["k3"],"conf_k4":conf["k4"],
                "checks":conf["checks"],"htf_blocked":conf["hard"],"htf_trend":htf.get("trend", "NEUTRAL"),
                "block_reason":"hard block" if conf["hard"] else "","mkt_score":conf["k4"]}

    for wp, wv in bid_walls:
        if wp >= price: continue
        dist = (price - wp) / price
        if dist > PROXIMITY_PCT: continue
        sig = _build("LONG", wp, wv, dist)
        if not sig["htf_blocked"] and sig["conf_total"] < CONF_WEAK: continue
        candidates.append(sig)

    for wp, wv in ask_walls:
        if wp <= price: continue
        dist = (wp - price) / price
        if dist > PROXIMITY_PCT: continue
        sig = _build("SHORT", wp, wv, dist)
        if not sig["htf_blocked"] and sig["conf_total"] < CONF_WEAK: continue
        candidates.append(sig)

    if not candidates: return []
    active = [s for s in candidates if not s["htf_blocked"]]
    blocked = [s for s in candidates if s["htf_blocked"]]
    has_long = any(s["dir"] == "LONG" for s in active)
    has_short = any(s["dir"] == "SHORT" for s in active)
    if has_long and has_short:
        best_long = max((s for s in active if s["dir"]=="LONG"), key=lambda x: x.get("conf_total", x.get("score",0)))
        best_short = max((s for s in active if s["dir"]=="SHORT"), key=lambda x: x.get("conf_total", x.get("score",0)))
        htf_trend = htf["trend"]
        if htf_trend == "BULL": winner = best_long
        elif htf_trend == "BEAR": winner = best_short
        elif best_long.get("conf_k4",0) != best_short.get("conf_k4",0): winner = best_long if best_long.get("conf_k4",0) > best_short.get("conf_k4",0) else best_short
        elif best_long.get("conf_total",0) != best_short.get("conf_total",0): winner = best_long if best_long.get("conf_total",0) > best_short.get("conf_total",0) else best_short
        elif best_long["dist_pct"] != best_short["dist_pct"]: winner = best_long if best_long["dist_pct"] < best_short["dist_pct"] else best_short
        else:
            rsi = float(df.iloc[-1]["rsi"]) if "rsi" in df.columns else 50
            winner = best_long if rsi < 50 else best_short
        loser = best_short if winner["dir"] == "LONG" else best_long
        wpts = winner.get("conf_total", winner.get("score",0)); lpts = loser.get("conf_total", loser.get("score",0))
        print(f"[CONFLICT] {winner['dir']} {wpts}pt won, {loser['dir']} {lpts}pt cancelled | HTF:{htf_trend}")
        active = [winner]
    signals = active + blocked
    signals.sort(key=lambda x: (x["htf_blocked"], -x["score"]))
    pending_dirs = {s["dir"] for s in _pending_signals if s.get("symbol") == SYMBOL}
    for sig in signals: sig["already_tracked"] = sig["dir"] in pending_dirs
    return signals

def _close_signal(sig, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price=None, close_reason=None):
    global _closed_signals
    duration_min = sig.get("_duration_override")
    if duration_min is None:
        try:
            open_dt = datetime.strptime(sig.get("ts","")[:8], "%H:%M:%S")
            close_dt = datetime.strptime(close_ts[:8], "%H:%M:%S")
            duration_min = max(0, int((close_dt - open_dt).seconds / 60))
        except: duration_min = None
    closed = {**sig, "outcome": outcome, "net_pnl_pct": net_pnl_pct,"net_pnl_usd": net_pnl_usd, "close_ts": close_ts,
              "exit_price": exit_price, "duration_min": duration_min,"close_reason": close_reason}
    _closed_signals.append(closed)
    _closed_signals = _closed_signals[-100:]
    db_id = sig.get("_db_id") or sig.get("id")
    if db_id:
        try: db_close_signal(db_id, outcome, net_pnl_pct, net_pnl_usd, close_ts, exit_price, duration_min, close_reason)
        except Exception as e: print(f"[DB ERROR] close_signal: {e}")
    print(f"[SIGNAL] {sig['dir']} {outcome} | {close_reason or '?'} | PnL:{net_pnl_pct}% | Exit:{exit_price}")

def check_pending_signals(df):
    global _pending_signals
    still_pending = []
    rt = 2 * COMMISSION
    now_ts = datetime.now().strftime("%H:%M:%S")
    last = df.iloc[-1]
    for sig in _pending_signals:
        is_long = sig["dir"] == "LONG"
        tp_hit = last["high"] >= sig["tp"] if is_long else last["low"] <= sig["tp"]
        sl_hit = last["low"] <= sig["sl"] if is_long else last["high"] >= sig["sl"]
        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                outcome = "WIN" if (is_long and last["close"] > sig["entry"]) or (not is_long and last["close"] < sig["entry"]) else "LOSS"
            elif tp_hit: outcome = "WIN"
            else: outcome = "LOSS"
            net_pnl_pct = round((TP_PCT - rt)*100, 2) if outcome == "WIN" else -round((SL_PCT + rt)*100, 2)
            net_pnl_usd = round(sig["entry"]*(TP_PCT - rt), 2) if outcome == "WIN" else -round(sig["entry"]*(SL_PCT + rt), 2)
            exit_price = sig["tp"] if outcome == "WIN" else sig["sl"]
            reason = "TP hit ✓" if outcome == "WIN" else "SL hit ✗"
            _close_signal(sig, outcome, net_pnl_pct, net_pnl_usd, now_ts, exit_price, reason)
        else: still_pending.append(sig)
    _pending_signals = still_pending

def calc_win_stats(symbol=None): return db_win_stats(symbol or SYMBOL)

def background_loop():
    global _pending_signals, _htf_cache, _htf_last_fetch, _mkt_cache, _mkt_last_fetch, _news_cache, _news_last_fetch, _tweet_cache, _tweet_last_fetch
    while True:
        try:
            now=time.time()
            if now-_htf_last_fetch>=HTF_REFRESH:
                try: _htf_cache=calc_htf_trend(fetch_htf_ohlcv()); _htf_last_fetch=now; print(f"[HTF] {_htf_cache['trend']} bull={_htf_cache['bull_sc']} bear={_htf_cache['bear_sc']}")
                except Exception as e: print(f"[HTF ERROR] {e}")
            if now-_mkt_last_fetch>=MKT_REFRESH:
                try: _mkt_cache=fetch_market_data(); _mkt_last_fetch=now; print(f"[MKT] FR={_mkt_cache['funding_rate']*100:.4f}% OI={_mkt_cache['oi_trend']} L/S={_mkt_cache['ls_ratio']} Taker={_mkt_cache['taker_ratio']:.2f}")
                except Exception as e: print(f"[MKT ERROR] {e}")
            if now-_news_last_fetch>=NEWS_REFRESH:
                try: _news_cache=fetch_news(); _news_last_fetch=now; print(f"[NEWS] {len(_news_cache)} news")
                except Exception as e: print(f"[NEWS ERROR] {e}")
            if now-_tweet_last_fetch>=TWEET_REFRESH:
                try: _tweet_cache=fetch_social(_tweet_keywords); _tweet_last_fetch=now; print(f"[Social] {len(_tweet_cache)} posts")
                except Exception as e: print(f"[Social ERROR] {e}")
            ticker=exchange.fetch_ticker(SYMBOL); price=float(ticker["last"]); change24h=float(ticker.get("percentage",0) or 0)
            ob=exchange.fetch_order_book(SYMBOL,OB_DEPTH); df=fetch_ohlcv(); df=load_indicators(df)
            bid_walls=cluster_walls(ob["bids"],price,TOP_WALLS); ask_walls=cluster_walls(ob["asks"],price,TOP_WALLS)
            signals=generate_signals(price,bid_walls,ask_walls,df); c=df.iloc[-1]
            candles_df=df.tail(60)[["open","high","low","close","volume","ema_fast","ema_slow","rsi","vol_ma"]].copy()
            candles_df[["ema_fast","ema_slow","rsi","vol_ma"]]=candles_df[["ema_fast","ema_slow","rsi","vol_ma"]].fillna(0)
            candles=candles_df.round(2).values.tolist()
            for sig in signals:
                if sig["htf_blocked"]: continue
                open_for_symbol = [p for p in _pending_signals if p.get("symbol") == SYMBOL]
                same_dir_open = [p for p in open_for_symbol if p["dir"] == sig["dir"]]
                opp_dir_open = [p for p in open_for_symbol if p["dir"] != sig["dir"]]
                if same_dir_open: continue
                if opp_dir_open:
                    existing = opp_dir_open[0]
                    is_long = sig["dir"] == "LONG"
                    rt = 2 * COMMISSION
                    raw_pct = (price - existing["entry"]) / existing["entry"] * (1 if is_long else -1)
                    net_pct = round((raw_pct - rt) * 100, 2)
                    net_usd = round(existing["entry"] * (raw_pct - rt), 2)
                    outcome = "WIN" if net_pct > 0 else "LOSS"
                    htf_reversed = (existing["dir"] == "LONG" and _htf_cache["trend"] == "BEAR") or (existing["dir"] == "SHORT" and _htf_cache["trend"] == "BULL")
                    stronger_signal = sig["score"] > existing.get("score", 0) + 1
                    mkt_against = sig["mkt_score"] >= 2
                    should_reverse = htf_reversed or (stronger_signal and mkt_against)
                    if not should_reverse: continue
                    reason = "HTF reversed" if htf_reversed else f"strong counter signal (★{sig['score']})"
                    now_ts = datetime.now().strftime("%H:%M:%S")
                    _close_signal(existing, outcome, net_pct, net_usd, now_ts, exit_price=price, close_reason=f"↩ Reverse — {reason}")
                    _pending_signals = [p for p in _pending_signals if not (p.get("symbol")==SYMBOL and p["dir"]==existing["dir"])]
                    print(f"[REVERSE] {existing['dir']} @ {existing['entry']} → {sig['dir']} | {reason} | P&L:{net_pct}%")
                new_sig = {**sig, "ts": datetime.now().strftime("%H:%M:%S"), "symbol": SYMBOL}
                try:
                    db_id = db_insert_signal(new_sig)
                    new_sig["_db_id"] = db_id
                except Exception as e: print(f"[DB ERROR] insert: {e}"); new_sig["_db_id"] = None
                _pending_signals.append(new_sig)
                print(f"[NEW SIGNAL] {sig['dir']} @ {sig['entry']} ★{sig['score']} DB:{new_sig['_db_id']}")
            stats=calc_win_stats(SYMBOL)
            check_pending_signals(df)
            new_state={"ts":datetime.now().strftime("%H:%M:%S"),"symbol":SYMBOL,"price":price,"change24h":round(change24h,2),"rsi":round(float(c["rsi"]),1),
                       "ema_fast":round(float(c["ema_fast"]),2),"ema_slow":round(float(c["ema_slow"]),2),
                       "vol_ratio":round(float(c["volume"]/c["vol_ma"]) if c["vol_ma"]>0 else 0,2),
                       "bid_walls":[{"price":round(p,2),"vol":round(v,2)} for p,v in bid_walls],
                       "ask_walls":[{"price":round(p,2),"vol":round(v,2)} for p,v in ask_walls],
                       "signals":signals,"candles":candles,"htf":_htf_cache,"mkt":_mkt_cache,
                       "news":_news_cache[:25],"tweets":_tweet_cache[:20],"tweet_kw":_tweet_keywords,
                       "pending":[{k:v for k,v in s.items() if k not in ("checks","entry_candle_idx","waited_count","_duration_override","_db_id")} for s in _pending_signals[-10:] if s.get("symbol")==SYMBOL],
                       "closed":list(reversed([s for s in _closed_signals[-20:] if s.get("symbol")==SYMBOL])),"stats":stats}
            with _lock: _state.update(new_state)
        except Exception as e: print(f"[ERROR] {e}")
        time.sleep(REFRESH_SEC)

# Minimal HTML template for AlwaysData
HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>BTC Signal Bot</title>
<style>body{background:#080c0f;color:#c8d8e8;font-family:monospace;padding:20px}
.header{display:flex;gap:20px;align-items:center;margin-bottom:20px}
.price{font-size:24px;color:#f0a500}
.up{color:#00d264}.down{color:#ff3d5a}
.panel{background:#0d1318;padding:15px;margin:10px 0;border-radius:5px}
.signal{padding:10px;margin:5px 0;border-left:3px solid #1e2d3a;background:#111820}
.signal.long{border-color:#00d264}.signal.short{border-color:#ff3d5a}
.badge{padding:2px 6px;border-radius:3px;font-size:11px}
.badge.win{background:rgba(0,210,100,.2);color:#00d264}.badge.loss{background:rgba(255,61,90,.2);color:#ff3d5a}
</style></head><body>
<div class="header"><h1>🚀 BTC/USDT Signal Bot</h1><span class="price" id="price">—</span><span id="change">—</span></div>
<div class="panel"><h3>📊 Indicators</h3><div id="indicators">Loading...</div></div>
<div class="panel"><h3>📈 HTF Trend</h3><div id="htf">Loading...</div></div>
<div class="panel"><h3>🏦 Market Data</h3><div id="market">Loading...</div></div>
<div class="panel"><h3>🎯 Active Signals</h3><div id="signals">No signals</div></div>
<div class="panel"><h3>⏳ Pending</h3><div id="pending">None</div></div>
<div class="panel"><h3>✅ Closed</h3><div id="closed">None</div></div>
<div class="panel"><h3>📊 Stats</h3><div id="stats">Loading...</div></div>
<script>
function fmt(n){return Number(n).toFixed(2);}
function stars(n){return'★'.repeat(n)+'☆'.repeat(4-n);}
const src=new EventSource('/stream');
src.onmessage=e=>{
  const d=JSON.parse(e.data);
  document.getElementById('price').textContent='$'+fmt(d.price||0);
  document.getElementById('change').textContent=(d.change24h>=0?'+':'')+d.change24h+'%';
  document.getElementById('change').className=d.change24h>=0?'up':'down';
  document.getElementById('indicators').innerHTML=`RSI:${d.rsi||'—'} | EMA:${d.ema_fast||'—'}/${d.ema_slow||'—'} | Vol:×${d.vol_ratio||'—'}`;
  const h=d.htf||{};document.getElementById('htf').innerHTML=`<span style="color:${h.trend==='BULL'?'#00d264':h.trend==='BEAR'?'#ff3d5a':'#f0a500'}">${h.trend||'—'} ${h.strength||0}/4</span> | RSI:${h.rsi||'—'}`;
  const m=d.mkt||{};document.getElementById('market').innerHTML=`Funding:${(m.funding_rate||0)*100}% | OI:${m.oi_trend||'—'} | L/S:${m.ls_ratio||'—'} | Taker:${m.taker_ratio||'—'}`;
  const s=d.signals||[];const active=s.filter(x=>!x.htf_blocked);
  document.getElementById('signals').innerHTML=active.length?active.map(sig=>`<div class="signal ${sig.dir.toLowerCase()}"><strong>${sig.dir}</strong> @ $${fmt(sig.entry)} | TP:$${fmt(sig.tp)} | SL:$${fmt(sig.sl)} | Score:${stars(sig.score)}</div>`).join(''):'No active signals';
  document.getElementById('pending').innerHTML=(d.pending||[]).length?(d.pending||[]).map(p=>`<div class="signal ${p.dir.toLowerCase()}">${p.dir} $${fmt(p.entry)} <span style="color:${(p.entry&&(d.price-p.entry)/(p.dir==='LONG'?1:-1)/p.entry*100)>=0?'#00d264':'#ff3d5a'}">(${((d.price-p.entry)/(p.dir==='LONG'?1:-1)/p.entry*100||0).toFixed(2)}%)</span></div>`).join(''):'None';
  document.getElementById('closed').innerHTML=(d.closed||[]).length?(d.closed||[]).map(c=>`<div class="signal ${c.outcome==='WIN'?'win':'loss'}"><span class="badge ${c.outcome.toLowerCase()}">${c.outcome}</span> ${c.dir} ${c.net_pnl_pct||0}%</div>`).join(''):'None';
  const st=d.stats||{};document.getElementById('stats').innerHTML=`Total:${st.total||0} | Wins:${st.wins||0} | Losses:${st.losses||0} | WinRate:${st.win_rate||0}% | PnL:${st.net_pnl_pct||0}%`;
};
</script></body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/stream")
def stream():
    def event_stream():
        last_ts = None
        while True:
            with _lock:
                ts = _state.get("ts")
                state = dict(_state)
            if ts and ts != last_ts:
                yield f"data: {json.dumps(state)}\n\n"
                last_ts = ts
            time.sleep(1)
    return Response(event_stream(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/clear_signals", methods=["POST"])
def clear_signals_route():
    global _pending_signals, _closed_signals
    data = flask_request.get_json(silent=True) or {}
    mode = data.get("mode", "all")
    symbol = data.get("symbol")
    def _fn(conn, m, sym):
        if m in ("all", "pending"):
            conn.execute("DELETE FROM signals WHERE status='pending'" + (" AND symbol=?" if sym else ""), (sym,) if sym else ())
        if m in ("all", "closed"):
            conn.execute("DELETE FROM signals WHERE status!='pending'" + (" AND symbol=?" if sym else ""), (sym,) if sym else ())
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    remaining = _db_write(_fn, mode, symbol)
    if mode in ("all", "pending"): _pending_signals = ([s for s in _pending_signals if s.get("symbol")!=symbol] if symbol else [])
    if mode in ("all", "closed"): _closed_signals = ([s for s in _closed_signals if s.get("symbol")!=symbol] if symbol else [])
    return {"ok": True, "remaining": remaining}

# WSGI Entry Point for AlwaysData
def create_app():
    """WSGI sunucuları için app factory."""
    load_signals()
    threading.Thread(target=background_loop, daemon=True).start()
    print("\n✅ Dashboard running → http://localhost:5000\n")
    return app

# Direct run
if __name__ == "__main__":
    load_signals()
    threading.Thread(target=background_loop, daemon=True).start()
    print("\n✅ Dashboard ready → http://localhost:5000\n")
    app.run(debug=False, port=5000, threaded=True, host="0.0.0.0")}