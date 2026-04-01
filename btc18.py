"""
BTC/USDT Sinyal Botu — Flask Dashboard (Tek Dosya)
===================================================
Yeni: Sinyal sonuç takibi (TP/SL) + Tutma oranı ekranı
Kurulum : pip install flask ccxt pandas ta
Çalıştır: python btc_dashboard.py → http://localhost:5000
"""

import json, time, threading, requests, re, html as html_lib
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, Response, render_template_string, request as flask_request
import ccxt, pandas as pd, ta

# ═══════════════════════════════════════════════════════════════
#  AYARLAR
# ═══════════════════════════════════════════════════════════════
SYMBOL         = "BTC/USDT"
AVAILABLE_SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT","DOGE/USDT"]
TIMEFRAME      = "5m"
CANDLE_LIMIT   = 100

# HTF (Üst Zaman Dilimi) ayarları
HTF_TIMEFRAME  = "1h"      # 5dk sinyalini 1 saatlik trendle filtrele
HTF_LIMIT      = 60        # 1s grafik geçmişi
HTF_EMA_FAST   = 20        # 1s hızlı EMA
HTF_EMA_SLOW   = 50        # 1s yavaş EMA
HTF_RSI_OB     = 60        # 1s RSI aşırı alım (biraz gevşek tutuyoruz)
HTF_RSI_OS     = 40        # 1s RSI aşırı satım
HTF_REFRESH    = 60        # HTF verisi kaç saniyede bir yenilenir

# ── Piyasa verisi ayarları ─────────────────────────────────────
BNFUT_BASE     = "https://fapi.binance.com"   # Binance Futures REST
MKT_REFRESH    = 30        # Piyasa verisi yenileme süresi (sn)

# Funding rate eşikleri
FUND_STRONG    = 0.0008    # %0.08 üstü → aşırı long (SHORT lehine)
FUND_WEAK      = -0.0008   # %0.08 altı → aşırı short (LONG lehine)

# Long/Short oran eşikleri (contrarian)
LS_CROWD_LONG  = 1.4       # > 1.4 kalabalık long → SHORT sinyali güçlenir
LS_CROWD_SHORT = 0.7       # < 0.7 kalabalık short → LONG sinyali güçlenir

# Taker hacim oranı eşiği
TAKER_STRONG   = 1.25      # Buy/Sell > 1.25 → agresif alıcılar
OI_CHANGE_THR  = 0.005     # OI %0.5 değişim eşiği (anlamlı)
OB_DEPTH       = 100       # Binance Futures: 5,10,20,50,100,500,1000
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
TP_PCT         = 0.02      # %2 brüt kar hedefi
SL_PCT         = 0.01      # %1 brüt zarar limiti
COMMISSION     = 0.0015    # %0.15 alım + %0.15 satım = %0.30 round-trip
MIN_SCORE      = 2
REFRESH_SEC    = 15
MAX_CANDLES_WAIT = 20

# ── Haber & Tweet ayarları ─────────────────────────────────
TWEET_REFRESH  = 60     # Tweet yenileme (saniye)
NEWS_REFRESH   = 120    # Haber yenileme (saniye)
NEWS_MAX       = 40     # Tutulacak maksimum haber
TWEET_MAX      = 30     # Tutulacak maksimum tweet

NEWS_FEEDS = [
    # Google News RSS — her zaman çalışır, auth gerekmez, symbol'e göre arama
    {"name": "Google News", "url": "https://news.google.com/rss/search?q={symbol}+crypto&hl=en-US&gl=US&ceid=US:en", "dynamic": True},
    # Sabit RSS kaynakları
    {"name": "CoinDesk",      "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt",       "url": "https://decrypt.co/feed"},
]

# StockTwits — ücretsiz, auth gerekmez, symbol bazlı sosyal akış
# BTC/USDT → BTC.X, ETH/USDT → ETH.X
STOCKTWITS_MAP = {
    "BTC":"BTC.X", "ETH":"ETH.X", "SOL":"SOL.X",
    "BNB":"BNB.X", "XRP":"XRP.X", "DOGE":"DOGE.X",
    "ADA":"ADA.X", "AVAX":"AVAX.X", "DOT":"DOT.X",
}
REDDIT_SUBS = ["Bitcoin","CryptoCurrency","btc","ethereum","CryptoMarkets","BitcoinMarkets"]
REDDIT_HEADERS = {"User-Agent": "btc-dashboard/1.0 (personal dashboard)", "Accept": "application/json"}

app      = Flask(__name__)
exchange = ccxt.binance({"options": {"defaultType": "future"}})

_lock            = threading.Lock()
_state           = {}
_pending_signals = []
_closed_signals  = []
_htf_cache       = {"trend": "NEUTRAL", "ema_fast": 0, "ema_slow": 0,
                    "rsi": 50, "score": 0, "details": [], "ts": None}
_htf_last_fetch  = 0

_mkt_cache = {
    "funding_rate" : 0.0, "funding_str"  : "bekleniyor",
    "oi_now"       : 0.0, "oi_prev"      : 0.0,
    "oi_change_pct": 0.0, "oi_trend"     : "bekleniyor",
    "ls_ratio"     : 1.0, "ls_str"       : "bekleniyor",
    "taker_buy"    : 0.0, "taker_sell"   : 0.0,
    "taker_ratio"  : 1.0, "taker_str"    : "bekleniyor",
    "ts"           : "—",
}
_mkt_last_fetch  = -999   # negatif → ilk döngüde hemen çekilsin

_news_cache      = []
_news_last_fetch = -999   # negatif → ilk döngüde hemen çekilsin

_tweet_cache      = []
_tweet_last_fetch = -999  # negatif → ilk döngüde hemen çekilsin
_tweet_keywords   = [SYMBOL.split("/")[0]]  # BTC/USDT → ["BTC"]

# ═══════════════════════════════════════════════════════════════
#  VERİ & HESAPLAMA
# ═══════════════════════════════════════════════════════════════
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

# ── HTF: 1 Saatlik Trend ─────────────────────────────────────
def fetch_htf_ohlcv():
    """1 saatlik OHLCV çeker. HTF_REFRESH saniyede bir çağrılır."""
    raw = exchange.fetch_ohlcv(SYMBOL, HTF_TIMEFRAME, limit=HTF_LIMIT)
    df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    return df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})

def calc_htf_trend(df_htf):
    """
    1s grafiğini analiz eder, şu 4 kriteri değerlendirir:
      1. EMA dizilimi  (EMA20 vs EMA50)
      2. EMA eğimi     (son 3 mumda EMA50 hangi yönde?)
      3. RSI bölgesi   (>60 → bullish, <40 → bearish)
      4. Fiyat konumu  (EMA50'nin üstünde / altında)

    Döndürür:
      trend    : "BULL" | "BEAR" | "NEUTRAL"
      strength : 0-4 (kaç kriter uyuşuyor)
      details  : açıklama listesi
    """
    df = df_htf.copy()
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], HTF_EMA_FAST).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], HTF_EMA_SLOW).ema_indicator()
    df["rsi"]      = ta.momentum.RSIIndicator(df["close"], 14).rsi()

    c  = df.iloc[-1]   # son mum
    p  = df.iloc[-2]   # önceki mum
    p2 = df.iloc[-4]   # 3 mum öncesi (eğim için)

    bull_score = 0
    bear_score = 0
    details    = []

    # 1. EMA dizilimi
    if c["ema_fast"] > c["ema_slow"]:
        bull_score += 1
        details.append({"label": f"1h EMA{HTF_EMA_FAST} > EMA{HTF_EMA_SLOW}", "side": "bull"})
    else:
        bear_score += 1
        details.append({"label": f"1h EMA{HTF_EMA_FAST} < EMA{HTF_EMA_SLOW}", "side": "bear"})

    # 2. EMA50 eğimi (son 3 mum)
    ema_slope = c["ema_slow"] - p2["ema_slow"]
    if ema_slope > 0:
        bull_score += 1
        details.append({"label": f"1h EMA{HTF_EMA_SLOW} yukarı eğimli", "side": "bull"})
    else:
        bear_score += 1
        details.append({"label": f"1h EMA{HTF_EMA_SLOW} aşağı eğimli", "side": "bear"})

    # 3. RSI bölgesi
    rsi = c["rsi"]
    if rsi > HTF_RSI_OB:
        bull_score += 1
        details.append({"label": f"1h RSI bullish bölge ({rsi:.0f})", "side": "bull"})
    elif rsi < HTF_RSI_OS:
        bear_score += 1
        details.append({"label": f"1h RSI bearish bölge ({rsi:.0f})", "side": "bear"})
    else:
        details.append({"label": f"1h RSI nötr ({rsi:.0f})", "side": "neutral"})

    # 4. Kapanış fiyatı EMA50'ye göre
    if c["close"] > c["ema_slow"]:
        bull_score += 1
        details.append({"label": f"1h fiyat EMA{HTF_EMA_SLOW} üstünde", "side": "bull"})
    else:
        bear_score += 1
        details.append({"label": f"1h fiyat EMA{HTF_EMA_SLOW} altında", "side": "bear"})

    # Karar
    if bull_score >= 3:
        trend = "BULL"
        strength = bull_score
    elif bear_score >= 3:
        trend = "BEAR"
        strength = bear_score
    else:
        trend = "NEUTRAL"
        strength = max(bull_score, bear_score)

    return {
        "trend"   : trend,
        "strength": strength,
        "bull_sc" : bull_score,
        "bear_sc" : bear_score,
        "ema_fast": round(float(c["ema_fast"]), 2),
        "ema_slow": round(float(c["ema_slow"]), 2),
        "rsi"     : round(float(rsi), 1),
        "details" : details,
        "ts"      : datetime.now().strftime("%H:%M:%S"),
    }


# ═══════════════════════════════════════════════════════════════
#  PİYASA VERİSİ — Binance Futures Public API
# ═══════════════════════════════════════════════════════════════
def _get(path, params=None, timeout=5):
    r = requests.get(BNFUT_BASE + path, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def fetch_market_data():
    """
    4 kaynaktan veri çeker:
      1. Funding Rate   — /fapi/v1/premiumIndex
      2. Open Interest  — /fapi/v1/openInterest + /futures/data/openInterestHist
      3. L/S Oranı      — /futures/data/globalLongShortAccountRatio
      4. Taker Hacmi    — /fapi/v1/klines (kolon 9 = taker buy base vol, kolon 5 = total vol)
                          Ayrı endpoint gerektirmez, kline verisi zaten bu bilgiyi içerir.
    """
    sym    = "BTCUSDT"
    result = dict(_mkt_cache)

    # 1. Funding Rate
    try:
        data = _get("/fapi/v1/premiumIndex", {"symbol": sym})
        fr   = float(data["lastFundingRate"])
        if fr > FUND_STRONG:
            fr_str = f"aşırı LONG pozitif ({fr*100:.4f}%)"
        elif fr < FUND_WEAK:
            fr_str = f"aşırı SHORT negatif ({fr*100:.4f}%)"
        elif fr > 0:
            fr_str = f"hafif pozitif ({fr*100:.4f}%)"
        else:
            fr_str = f"hafif negatif ({fr*100:.4f}%)"
        result["funding_rate"] = round(fr, 6)
        result["funding_str"]  = fr_str
    except Exception as e:
        print(f"[MKT/Funding] {e}")

    # 2. Open Interest
    try:
        oi_now  = float(_get("/fapi/v1/openInterest", {"symbol": sym})["openInterest"])
        history = _get("/futures/data/openInterestHist",
                       {"symbol": sym, "period": "5m", "limit": 6})
        oi_prev = float(history[0]["sumOpenInterest"]) if history else oi_now
        oi_chg  = (oi_now - oi_prev) / oi_prev if oi_prev > 0 else 0
        oi_trend = "nötr"
        if abs(oi_chg) >= OI_CHANGE_THR:
            oi_trend = "artıyor" if oi_chg > 0 else "azalıyor"
        result["oi_now"]        = round(oi_now, 0)
        result["oi_prev"]       = round(oi_prev, 0)
        result["oi_change_pct"] = round(oi_chg * 100, 3)
        result["oi_trend"]      = oi_trend
    except Exception as e:
        print(f"[MKT/OI] {e}")

    # 3. Long/Short Oranı
    try:
        ls_data = _get("/futures/data/globalLongShortAccountRatio",
                       {"symbol": sym, "period": "5m", "limit": 1})
        ls = float(ls_data[0]["longShortRatio"]) if ls_data else 1.0
        if ls > LS_CROWD_LONG:
            ls_str = f"kalabalık LONG ({ls:.2f})"
        elif ls < LS_CROWD_SHORT:
            ls_str = f"kalabalık SHORT ({ls:.2f})"
        else:
            ls_str = f"dengeli ({ls:.2f})"
        result["ls_ratio"] = round(ls, 3)
        result["ls_str"]   = ls_str
    except Exception as e:
        print(f"[MKT/LS] {e}")

    # 4. Taker Buy/Sell Hacmi — kline verisi kullanılır (kolon 5=total, 9=taker_buy)
    # Binance Futures kline: [open_time, open, high, low, close, volume,
    #   close_time, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]
    try:
        klines = _get("/fapi/v1/klines",
                      {"symbol": sym, "interval": "5m", "limit": 6})
        if klines:
            total_vol     = sum(float(k[5]) for k in klines)
            taker_buy_vol = sum(float(k[9]) for k in klines)
            taker_sel_vol = total_vol - taker_buy_vol
            tk_ratio = taker_buy_vol / taker_sel_vol if taker_sel_vol > 0 else 1.0
            if tk_ratio > TAKER_STRONG:
                tk_str = f"agresif alıcılar ({tk_ratio:.2f})"
            elif tk_ratio < (1 / TAKER_STRONG):
                tk_str = f"agresif satıcılar ({tk_ratio:.2f})"
            else:
                tk_str = f"dengeli ({tk_ratio:.2f})"
            result["taker_buy"]   = round(taker_buy_vol, 2)
            result["taker_sell"]  = round(taker_sel_vol, 2)
            result["taker_ratio"] = round(tk_ratio, 3)
            result["taker_str"]   = tk_str
    except Exception as e:
        print(f"[MKT/Taker] {e}")

    result["ts"] = datetime.now().strftime("%H:%M:%S")
    return result


def score_market_data(direction):
    """
    SERT FİLTRE MODU — 4 metriğin her biri bağımsız değerlendirilir.

    Kural:
      • Nötr (belirsiz) → geçer, sinyale etkisi yok
      • Lehte           → geçer, checks'e ✓ eklenir
      • Aleyhte         → hard_block = True, sinyal üretilmez

    Döndürür: (score_bonus, checks, hard_block)
      score_bonus: lehte metrik başına +1 (toplam max 4)
      hard_block : herhangi bir metrik aleyhte ise True
    """
    mkt     = _mkt_cache
    is_long = direction == "LONG"
    bonus   = 0
    checks  = []
    hard    = False

    # ── 1. Funding Rate ─────────────────────────────────────────
    # Mantık: Kalabalık LONG pozisyon tutuyorsa funding pozitif olur.
    # Traderlar uzun süre ödeme yapmak istemez → fiyat LONG'a ters döner.
    # Negatif funding → herkes short sıkışmış → LONG lehine
    # Pozitif funding → herkes long sıkışmış  → SHORT lehine
    fr = mkt["funding_rate"]
    if is_long:
        if fr > FUND_STRONG:          # çok fazla long pozisyon → tehlike
            hard = True
            checks.append({"label": f"🚫 Funding yüksek → kalabalık LONG ({fr*100:.4f}%)",
                            "status":"fail","side":"short"})
        elif fr < FUND_WEAK:          # kalabalık short → contrarian LONG avantajı
            bonus += 1
            checks.append({"label": f"✓ Funding negatif → LONG lehine ({fr*100:.4f}%)",
                            "status":"pass","side":"long"})
        else:
            checks.append({"label": f"· Funding nötr ({fr*100:.4f}%)",
                            "status":"warn","side":"neutral"})
    else:  # SHORT
        if fr < FUND_WEAK:            # çok fazla short pozisyon → tehlike
            hard = True
            checks.append({"label": f"🚫 Funding negatif → kalabalık SHORT ({fr*100:.4f}%)",
                            "status":"fail","side":"long"})
        elif fr > FUND_STRONG:        # kalabalık long → contrarian SHORT avantajı
            bonus += 1
            checks.append({"label": f"✓ Funding pozitif → SHORT lehine ({fr*100:.4f}%)",
                            "status":"pass","side":"short"})
        else:
            checks.append({"label": f"· Funding nötr ({fr*100:.4f}%)",
                            "status":"warn","side":"neutral"})

    # ── 2. Open Interest ────────────────────────────────────────
    # OI artıyor + aynı yön → yeni para giriyor, trend güçleniyor
    # OI azalıyor           → pozisyonlar kapanıyor, trend zayıflıyor → engelle
    oi_chg   = mkt["oi_change_pct"]
    oi_trend = mkt["oi_trend"]
    if oi_trend == "azalıyor":
        hard = True
        checks.append({"label": f"🚫 OI azalıyor ({oi_chg:.2f}%) → trend zayıflıyor",
                        "status":"fail","side":""})
    elif oi_trend == "artıyor":
        bonus += 1
        side = "long" if is_long else "short"
        checks.append({"label": f"✓ OI artıyor +{oi_chg:.2f}% → yeni pozisyon girişi",
                        "status":"pass","side":side})
    else:
        checks.append({"label": f"· OI nötr ({oi_chg:+.2f}%)",
                        "status":"warn","side":"neutral"})

    # ── 3. Long/Short Oranı (contrarian) ────────────────────────
    # Çoğunluk her zaman yanlış yaptayken yakıt tükenir → ters döner
    # Kalabalık LONG  → SHORT sinyali güçlenir, LONG engellenir
    # Kalabalık SHORT → LONG sinyali güçlenir, SHORT engellenir
    ls = mkt["ls_ratio"]
    if is_long:
        if ls > LS_CROWD_LONG:        # herkes long → LONG için tehlikeli
            hard = True
            checks.append({"label": f"🚫 Kalabalık LONG → contrarian risk ({ls:.2f})",
                            "status":"fail","side":"short"})
        elif ls < LS_CROWD_SHORT:     # herkes short → LONG için fırsat
            bonus += 1
            checks.append({"label": f"✓ Kalabalık SHORT → LONG fırsatı ({ls:.2f})",
                            "status":"pass","side":"long"})
        else:
            checks.append({"label": f"· L/S dengeli ({ls:.2f})",
                            "status":"warn","side":"neutral"})
    else:  # SHORT
        if ls < LS_CROWD_SHORT:       # herkes short → SHORT için tehlikeli
            hard = True
            checks.append({"label": f"🚫 Kalabalık SHORT → contrarian risk ({ls:.2f})",
                            "status":"fail","side":"long"})
        elif ls > LS_CROWD_LONG:      # herkes long → SHORT için fırsat
            bonus += 1
            checks.append({"label": f"✓ Kalabalık LONG → SHORT fırsatı ({ls:.2f})",
                            "status":"pass","side":"short"})
        else:
            checks.append({"label": f"· L/S dengeli ({ls:.2f})",
                            "status":"warn","side":"neutral"})

    # ── 4. Taker Buy/Sell Hacmi ─────────────────────────────────
    # Taker = piyasa emri veren, yani agresif taraf
    # Agresif alıcılar → fiyatı yukarı iter → LONG lehine
    # Agresif satıcılar → fiyatı aşağı iter → SHORT lehine
    # Ters yönde agresiflik → sinyal engellenir
    tk = mkt["taker_ratio"]
    if is_long:
        if tk < 1 / TAKER_STRONG:     # agresif satıcılar baskın → LONG engelle
            hard = True
            checks.append({"label": f"🚫 Agresif satıcılar → LONG aleyhte (×{tk:.2f})",
                            "status":"fail","side":"short"})
        elif tk > TAKER_STRONG:       # agresif alıcılar → LONG lehine
            bonus += 1
            checks.append({"label": f"✓ Agresif alıcılar → LONG lehine (×{tk:.2f})",
                            "status":"pass","side":"long"})
        else:
            checks.append({"label": f"· Taker dengeli (×{tk:.2f})",
                            "status":"warn","side":"neutral"})
    else:  # SHORT
        if tk > TAKER_STRONG:         # agresif alıcılar baskın → SHORT engelle
            hard = True
            checks.append({"label": f"🚫 Agresif alıcılar → SHORT aleyhte (×{tk:.2f})",
                            "status":"fail","side":"long"})
        elif tk < 1 / TAKER_STRONG:   # agresif satıcılar → SHORT lehine
            bonus += 1
            checks.append({"label": f"✓ Agresif satıcılar → SHORT lehine (×{tk:.2f})",
                            "status":"pass","side":"short"})
        else:
            checks.append({"label": f"· Taker dengeli (×{tk:.2f})",
                            "status":"warn","side":"neutral"})

    return bonus, checks, hard


# ═══════════════════════════════════════════════════════════════
#  HABER & TWEET AKIŞI
# ═══════════════════════════════════════════════════════════════
_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BTC-Dashboard/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

def _parse_rss_time(s):
    if not s: return ""
    from datetime import timezone
    fmts = ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt.astimezone(timezone.utc).strftime("%H:%M")
        except Exception:
            pass
    return s[:5] if len(s) >= 5 else s

def _strip_html(text):
    text = html_lib.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()[:220]

def fetch_news():
    """
    Google News RSS (symbol-aware) + sabit kripto kaynaklar.
    Hiç auth gerekmez.
    """
    items = []
    sym_base = SYMBOL.split("/")[0]  # BTC/USDT → BTC

    for feed in NEWS_FEEDS:
        try:
            url = feed["url"]
            if feed.get("dynamic"):
                # Google News: symbol adı + kripto anahtar kelimesi
                query = requests.utils.quote(f"{sym_base} crypto price")
                url   = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

            r    = requests.get(url, headers=_RSS_HEADERS, timeout=8)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ns   = {"atom": "http://www.w3.org/2005/Atom"}

            for item in root.findall(".//item")[:10]:
                title   = _strip_html(item.findtext("title",""))
                link    = (item.findtext("link") or "").strip()
                pubdate = _parse_rss_time(item.findtext("pubDate",""))
                raw     = item.findtext("pubDate","") or ""
                # Google News link'i redirect URL'dir — description'dan gerçek linki al
                if "news.google.com" in link:
                    # Google News başlık temizle (kaynak adı içerebilir)
                    title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()
                if title:
                    items.append({"title":title,"source":feed["name"],
                                  "url":link,"ts":pubdate,"raw_ts":raw})
        except Exception as e:
            print(f"[RSS/{feed['name']}] {e}")

    items.sort(key=lambda x: x.get("raw_ts",""), reverse=True)
    # Duplikat başlık temizle
    seen, unique = set(), []
    for it in items:
        key = it["title"][:50]
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique[:NEWS_MAX]


def fetch_social(keywords):
    """
    StockTwits JSON API: her zaman SYMBOL stream'ini çeker.
    keywords ek bağlam için tutulur ama StockTwits symbol'den sapılmaz.
    Fallback: Reddit (symbol adıyla arama).
    """
    items    = []
    sym_base = SYMBOL.split("/")[0]
    st_sym   = STOCKTWITS_MAP.get(sym_base, sym_base + ".X")

    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{st_sym}.json"
        r   = requests.get(url, timeout=8,
                           headers={"User-Agent": "btc-dashboard/1.0"})
        r.raise_for_status()
        data = r.json()

        for msg in data.get("messages", [])[:TWEET_MAX]:
            body    = _strip_html(msg.get("body",""))
            user    = msg.get("user",{}).get("username","?")
            created = msg.get("created_at","")
            url_msg = f"https://stocktwits.com/{user}"
            sentiment = msg.get("entities",{}).get("sentiment",{})
            sent_str  = ""
            if sentiment:
                sv = sentiment.get("basic","")
                if sv == "Bullish":   sent_str = " 🟢"
                elif sv == "Bearish": sent_str = " 🔴"

            # Zaman
            ts_fmt = ""
            raw_ts = created
            if created:
                try:
                    from datetime import timezone
                    dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
                    dt = dt.replace(tzinfo=timezone.utc)
                    ts_fmt = dt.strftime("%H:%M")
                except Exception:
                    ts_fmt = created[:5]

            if body:
                items.append({
                    "text"  : body + sent_str,
                    "user"  : "@" + user,
                    "url"   : url_msg,
                    "ts"    : ts_fmt,
                    "raw_ts": raw_ts,
                    "score" : 0,
                    "comms" : 0,
                })

        print(f"[StockTwits] {len(items)} mesaj ({st_sym})")

    except Exception as e:
        print(f"[StockTwits] {e}")
        # Fallback: Reddit
        try:
            q   = sym_base if not keywords else " OR ".join(keywords[:3])
            sub = "+".join(REDDIT_SUBS[:4])
            url = f"https://www.reddit.com/r/{sub}/search.json"
            r   = requests.get(url, headers=REDDIT_HEADERS,
                               params={"q":q,"sort":"new","limit":20,"t":"day"}, timeout=8)
            r.raise_for_status()
            posts = r.json().get("data",{}).get("children",[])
            for post in posts:
                p       = post.get("data",{})
                title   = _strip_html(p.get("title",""))
                author  = p.get("author","")
                score   = p.get("score",0)
                comms   = p.get("num_comments",0)
                created = p.get("created_utc",0)
                link    = "https://reddit.com" + p.get("permalink","")
                ts_fmt  = ""
                raw_ts  = ""
                if created:
                    from datetime import timezone
                    dt = datetime.utcfromtimestamp(created).replace(tzinfo=timezone.utc)
                    ts_fmt = dt.strftime("%H:%M")
                    raw_ts = dt.isoformat()
                if title:
                    items.append({"text":title,"user":f"u/{author}",
                                  "url":link,"ts":ts_fmt,"raw_ts":raw_ts,
                                  "score":score,"comms":comms})
            print(f"[Reddit fallback] {len(items)} post")
        except Exception as e2:
            print(f"[Reddit fallback] {e2}")

    items.sort(key=lambda x: x.get("raw_ts",""), reverse=True)
    return items[:TWEET_MAX]


def cluster_walls(orders, ref, n):
    buckets = {}
    for price, qty in orders:
        b = round(price / (ref * BUCKET_PCT)) * (ref * BUCKET_PCT)
        buckets[b] = buckets.get(b, 0) + qty
    buckets = {p: v for p, v in buckets.items() if v >= MIN_WALL_BTC}
    top = sorted(buckets.items(), key=lambda x: x[1], reverse=True)[:n]
    return sorted(top, key=lambda x: x[0])

def detect_candle(df, is_long):
    c=df.iloc[-1]; p=df.iloc[-2]
    bs=c["body_size"]; wd=c["wick_down"]; wu=c["wick_up"]; b=c["body"]
    if is_long:
        if wd>bs*2 and wu<bs*0.5:                         return "Hammer 🔨"
        if b>0 and p["body"]<0 and bs>abs(p["body"])*1.2: return "Bullish Engulfing 📈"
        if len(df)>=3:
            pp=df.iloc[-3]
            if pp["body"]<0 and abs(p["body"])<abs(pp["body"])*0.4 and b>0: return "Morning Star ⭐"
        if b>0 and wd>bs*1.5:                             return "Bullish Pin Bar 📌"
    else:
        if wu>bs*2 and wd<bs*0.5:                         return "Shooting Star 💫"
        if b<0 and p["body"]>0 and bs>p["body_size"]*1.2: return "Bearish Engulfing 📉"
        if len(df)>=3:
            pp=df.iloc[-3]
            if pp["body"]>0 and abs(p["body"])<abs(pp["body"])*0.4 and b<0: return "Evening Star ⭐"
        if b<0 and wu>bs*1.5:                             return "Bearish Pin Bar 📌"
    return None

def score_reversal(df, direction):
    score=0; checks=[]
    c=df.iloc[-1]; p=df.iloc[-2]
    is_long=direction=="LONG"

    cb=(p["ema_fast"]<p["ema_slow"]) and (c["ema_fast"]>c["ema_slow"])
    cs=(p["ema_fast"]>p["ema_slow"]) and (c["ema_fast"]<c["ema_slow"])
    ba=c["ema_fast"]>c["ema_slow"]
    if is_long and (cb or ba):
        score+=1; checks.append({"label":f"EMA{EMA_FAST}>{EMA_SLOW}"+(" kesişim" if cb else ""),"status":"pass","side":"long"})
    elif not is_long and (cs or not ba):
        score+=1; checks.append({"label":f"EMA{EMA_FAST}<{EMA_SLOW}"+(" kesişim" if cs else ""),"status":"pass","side":"short"})
    else:
        checks.append({"label":"EMA aleyhte","status":"fail","side":""})

    rsi=c["rsi"]; div=False
    if is_long and len(df)>=5:
        lp=df["low"].iloc[-5:]; lr=df["rsi"].iloc[-5:]
        if lp.iloc[-1]<=lp.min() and lr.iloc[-1]>lr.min(): div=True
    elif not is_long and len(df)>=5:
        hp=df["high"].iloc[-5:]; hr=df["rsi"].iloc[-5:]
        if hp.iloc[-1]>=hp.max() and hr.iloc[-1]<hr.max(): div=True

    if is_long and (rsi<RSI_OS or div):
        score+=1; checks.append({"label":f"RSI {'diverjans' if div else 'aşırı satım'} ({rsi:.0f})","status":"pass","side":"long"})
    elif not is_long and (rsi>RSI_OB or div):
        score+=1; checks.append({"label":f"RSI {'diverjans' if div else 'aşırı alım'} ({rsi:.0f})","status":"pass","side":"short"})
    else:
        checks.append({"label":f"RSI nötr ({rsi:.0f})","status":"fail","side":""})

    vr=(c["volume"]/c["vol_ma"]) if c["vol_ma"]>0 else 0
    if vr>=VOL_MULTIPLIER:
        score+=1; checks.append({"label":f"Hacim spike ×{vr:.1f}","status":"pass","side":"neutral"})
    else:
        checks.append({"label":f"Hacim normal ×{vr:.1f}","status":"fail","side":""})

    pat=detect_candle(df, is_long)
    if pat:
        score+=1; checks.append({"label":pat,"status":"pass","side":"long" if is_long else "short"})
    else:
        checks.append({"label":"Net formasyon yok","status":"fail","side":""})

    return score, checks

def generate_signals(price, bid_walls, ask_walls, df):
    signals = []
    htf     = _htf_cache

    def _build_signal(direction, wp, wv, dist):
        is_long = direction == "LONG"
        sc, ch  = score_reversal(df, direction)

        # ── HTF katmanı ──────────────────────────────────────────
        htf_ok = htf["trend"] != ("BEAR" if is_long else "BULL")
        if is_long:
            if htf["trend"] == "BULL":
                ch.append({"label": f"1h trend BULL ✓ ({htf['strength']}/4)", "status":"pass","side":"long"})
                sc += 1
            elif htf["trend"] == "NEUTRAL":
                ch.append({"label": f"1h trend NÖTR ({htf['strength']}/4)", "status":"warn","side":"neutral"})
            else:
                ch.append({"label": "1h trend BEAR — LONG engellendi", "status":"fail","side":"short"})
        else:
            if htf["trend"] == "BEAR":
                ch.append({"label": f"1h trend BEAR ✓ ({htf['strength']}/4)", "status":"pass","side":"short"})
                sc += 1
            elif htf["trend"] == "NEUTRAL":
                ch.append({"label": f"1h trend NÖTR ({htf['strength']}/4)", "status":"warn","side":"neutral"})
            else:
                ch.append({"label": "1h trend BULL — SHORT engellendi", "status":"fail","side":"long"})

        # ── Piyasa verisi katmanı ─────────────────────────────────
        mkt_sc, mkt_ch, mkt_hard = score_market_data(direction)
        sc   += mkt_sc
        ch   += mkt_ch

        # Engel kontrolü: HTF bloğu VEYA piyasa verisi sert bloğu
        blocked     = not htf_ok or mkt_hard
        block_reason = []
        if not htf_ok:
            block_reason.append(f"1h {htf['trend']}")
        if mkt_hard:
            block_reason.append("piyasa verisi")

        tp       = round(price * (1 + TP_PCT if is_long else 1 - TP_PCT), 2)
        sl       = round(price * (1 - SL_PCT if is_long else 1 + SL_PCT), 2)
        rt       = 2 * COMMISSION
        return {
            "dir"         : direction,
            "entry"       : price,
            "tp"          : tp,
            "sl"          : sl,
            "net_tp_pct"  : round((TP_PCT - rt) * 100, 2),
            "net_sl_pct"  : round((SL_PCT + rt) * 100, 2),
            "net_tp_usd"  : round(price * (TP_PCT - rt), 2),
            "net_sl_usd"  : round(price * (SL_PCT + rt), 2),
            "comm_usd"    : round(price * rt, 2),
            "wall_price"  : wp,
            "wall_vol"    : round(wv, 2),
            "dist_pct"    : round(dist * 100, 3),
            "score"       : sc,
            "checks"      : ch,
            "htf_blocked" : blocked,
            "htf_trend"   : htf["trend"],
            "block_reason": " + ".join(block_reason) if block_reason else "",
            "mkt_score"   : mkt_sc,
        }

    for wp, wv in bid_walls:
        if wp >= price: continue
        dist = (price - wp) / price
        if dist > PROXIMITY_PCT: continue
        sig = _build_signal("LONG", wp, wv, dist)
        if not sig["htf_blocked"] and sig["score"] < MIN_SCORE:
            continue
        signals.append(sig)

    for wp, wv in ask_walls:
        if wp <= price: continue
        dist = (wp - price) / price
        if dist > PROXIMITY_PCT: continue
        sig = _build_signal("SHORT", wp, wv, dist)
        if not sig["htf_blocked"] and sig["score"] < MIN_SCORE:
            continue
        signals.append(sig)

    signals.sort(key=lambda x: (x["htf_blocked"], -x["score"]))
    return signals

# ═══════════════════════════════════════════════════════════════
#  SİNYAL SONUÇ TAKİBİ
# ═══════════════════════════════════════════════════════════════
def check_pending_signals(df):
    """
    Bekleyen sinyaller için OHLCV mumlarına bakarak TP/SL çakışması kontrol eder.
    Mum high >= TP  → TP vurdu (WIN)
    Mum low  <= SL  → SL vurdu (LOSS)
    Her mumda sırayla kontrol edilir (hangisi önce tetiklendi)
    """
    global _pending_signals, _closed_signals

    still_pending = []
    for sig in _pending_signals:
        entry_idx = sig.get("entry_candle_idx", len(df)-1)
        # Sinyal sonrası gelen mumları al
        future_candles = df.iloc[entry_idx+1:]

        resolved = False
        for _, row in future_candles.iterrows():
            is_long = sig["dir"] == "LONG"
            tp_hit  = row["high"] >= sig["tp"] if is_long else row["low"] <= sig["tp"]
            sl_hit  = row["low"]  <= sig["sl"] if is_long else row["high"] >= sig["sl"]

            if tp_hit and sl_hit:
                # Aynı mumda ikisi de vurmuşsa gövde yönüne bak
                outcome = "WIN" if (is_long and row["close"] > sig["entry"]) or \
                                   (not is_long and row["close"] < sig["entry"]) else "LOSS"
            elif tp_hit:
                outcome = "WIN"
            elif sl_hit:
                outcome = "LOSS"
            else:
                continue

            # Net P&L (komisyon düşüldükten sonra)
            rt_comm = 2 * COMMISSION  # round-trip komisyon
            if outcome == "WIN":
                net_pnl_pct = round((TP_PCT - rt_comm) * 100, 2)   # +1.70%
                net_pnl_usd = round(sig["entry"] * (TP_PCT - rt_comm), 2)
            else:
                net_pnl_pct = -round((SL_PCT + rt_comm) * 100, 2)  # -1.30%
                net_pnl_usd = -round(sig["entry"] * (SL_PCT + rt_comm), 2)

            closed = {**sig, "outcome": outcome,
                      "net_pnl_pct": net_pnl_pct,
                      "net_pnl_usd": net_pnl_usd,
                      "close_ts": datetime.now().strftime("%H:%M:%S")}
            _closed_signals.append(closed)
            _closed_signals = _closed_signals[-100:]
            resolved = True
            break

        # Çok uzun süredir bekliyorsa zaman aşımı
        candles_waited = len(df) - 1 - entry_idx
        if not resolved:
            if candles_waited >= MAX_CANDLES_WAIT:
                # Mevcut fiyata göre kâr/zarar değerlendir
                current = df.iloc[-1]["close"]
                is_long = sig["dir"] == "LONG"
                outcome = "WIN" if (is_long and current > sig["entry"]) or \
                                   (not is_long and current < sig["entry"]) else "LOSS"
                rt_comm = 2 * COMMISSION
                if outcome == "WIN":
                    net_pnl_pct = round((TP_PCT - rt_comm) * 100, 2)
                    net_pnl_usd = round(sig["entry"] * (TP_PCT - rt_comm), 2)
                else:
                    net_pnl_pct = -round((SL_PCT + rt_comm) * 100, 2)
                    net_pnl_usd = -round(sig["entry"] * (SL_PCT + rt_comm), 2)
                closed = {**sig, "outcome": outcome,
                          "net_pnl_pct": net_pnl_pct,
                          "net_pnl_usd": net_pnl_usd,
                          "close_ts": datetime.now().strftime("%H:%M:%S") + " (timeout)"}
                _closed_signals.append(closed)
                _closed_signals = _closed_signals[-100:]
            else:
                still_pending.append(sig)

    _pending_signals = still_pending

def calc_win_stats():
    if not _closed_signals:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,
                "long_total":0,"long_wins":0,"long_rate":0,
                "short_total":0,"short_wins":0,"short_rate":0,
                "net_pnl_pct":0,"net_pnl_usd":0,
                "comm_pct": round(2*COMMISSION*100,2)}
    wins   = sum(1 for s in _closed_signals if s["outcome"]=="WIN")
    losses = len(_closed_signals) - wins
    longs  = [s for s in _closed_signals if s["dir"]=="LONG"]
    shorts = [s for s in _closed_signals if s["dir"]=="SHORT"]
    lw     = sum(1 for s in longs  if s["outcome"]=="WIN")
    sw     = sum(1 for s in shorts if s["outcome"]=="WIN")
    net_pnl_pct = round(sum(s.get("net_pnl_pct",0) for s in _closed_signals), 2)
    net_pnl_usd = round(sum(s.get("net_pnl_usd",0) for s in _closed_signals), 2)
    return {
        "total"      : len(_closed_signals),
        "wins"       : wins,
        "losses"     : losses,
        "win_rate"   : round(wins/len(_closed_signals)*100,1),
        "long_total" : len(longs),
        "long_wins"  : lw,
        "long_rate"  : round(lw/len(longs)*100,1) if longs else 0,
        "short_total": len(shorts),
        "short_wins" : sw,
        "short_rate" : round(sw/len(shorts)*100,1) if shorts else 0,
        "net_pnl_pct": net_pnl_pct,
        "net_pnl_usd": net_pnl_usd,
        "comm_pct"   : round(2*COMMISSION*100,2),
    }

# ═══════════════════════════════════════════════════════════════
#  ARKA PLAN DÖNGÜSÜ
# ═══════════════════════════════════════════════════════════════
def background_loop():
    global _pending_signals, _htf_cache, _htf_last_fetch, _mkt_cache, _mkt_last_fetch
    global _news_cache, _news_last_fetch, _tweet_cache, _tweet_last_fetch
    while True:
        try:
            now = time.time()

            # ── HTF: her 60 saniyede bir ────────────────────────
            if now - _htf_last_fetch >= HTF_REFRESH:
                try:
                    df_htf       = fetch_htf_ohlcv()
                    _htf_cache   = calc_htf_trend(df_htf)
                    _htf_last_fetch = now
                    print(f"[HTF] {_htf_cache['trend']}  bull={_htf_cache['bull_sc']} bear={_htf_cache['bear_sc']}  RSI={_htf_cache['rsi']}")
                except Exception as e:
                    print(f"[HTF Hata] {e}")

            # ── Piyasa verisi: her 30 saniyede bir ──────────────
            if now - _mkt_last_fetch >= MKT_REFRESH:
                try:
                    _mkt_cache      = fetch_market_data()
                    _mkt_last_fetch = now
                    print(f"[MKT] FR={_mkt_cache['funding_rate']*100:.4f}%  "
                          f"OI={_mkt_cache['oi_trend']}  "
                          f"L/S={_mkt_cache['ls_ratio']}  "
                          f"Taker={_mkt_cache['taker_ratio']:.2f}")
                except Exception as e:
                    print(f"[MKT Hata] {e}")

            # ── Haberler: her 120 saniyede bir ──────────────────
            if now - _news_last_fetch >= NEWS_REFRESH:
                try:
                    _news_cache      = fetch_news()
                    _news_last_fetch = now
                    print(f"[NEWS] {len(_news_cache)} haber yüklendi")
                except Exception as e:
                    print(f"[NEWS Hata] {e}")

            # ── Tweetler: her 90 saniyede bir ───────────────────
            if now - _tweet_last_fetch >= TWEET_REFRESH:
                try:
                    _tweet_cache      = fetch_social(_tweet_keywords)
                    _tweet_last_fetch = now
                    print(f"[Social] {len(_tweet_cache)} post ({', '.join(_tweet_keywords) if _tweet_keywords else 'hot'})")
                except Exception as e:
                    print(f"[Social Hata] {e}")

            # ── 5dk verisi + sinyaller ───────────────────────────────
            ticker    = exchange.fetch_ticker(SYMBOL)
            price     = float(ticker["last"])
            change24h = float(ticker.get("percentage",0) or 0)
            ob        = exchange.fetch_order_book(SYMBOL, OB_DEPTH)
            df        = fetch_ohlcv()
            df        = calc_indicators(df)
            bid_walls = cluster_walls(ob["bids"], price, TOP_WALLS)
            ask_walls = cluster_walls(ob["asks"], price, TOP_WALLS)
            signals   = generate_signals(price, bid_walls, ask_walls, df)
            c         = df.iloc[-1]
            candles   = df.tail(60)[["open","high","low","close","volume",
                                      "ema_fast","ema_slow","rsi","vol_ma"]].round(2).fillna(0).values.tolist()

            # Yeni sinyalleri bekleme listesine ekle
            for sig in signals:
                already = any(
                    p["dir"]==sig["dir"] and abs(p["entry"]-sig["entry"])<50
                    for p in _pending_signals
                )
                if not already:
                    _pending_signals.append({
                        **sig,
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "entry_candle_idx": len(df)-1
                    })

            # Bekleyen sinyallerin sonuçlarını kontrol et
            check_pending_signals(df)
            stats = calc_win_stats()

            new_state = {
                "ts"           : datetime.now().strftime("%H:%M:%S"),
                "symbol"       : SYMBOL,
                "price"        : price,
                "change24h"    : round(change24h,2),
                "rsi"          : round(float(c["rsi"]),1),
                "ema_fast"     : round(float(c["ema_fast"]),2),
                "ema_slow"     : round(float(c["ema_slow"]),2),
                "vol_ratio"    : round(float(c["volume"]/c["vol_ma"]) if c["vol_ma"]>0 else 0,2),
                "bid_walls"    : [{"price":round(p,2),"vol":round(v,2)} for p,v in bid_walls],
                "ask_walls"    : [{"price":round(p,2),"vol":round(v,2)} for p,v in ask_walls],
                "signals"      : signals,
                "candles"      : candles,
                "htf"          : _htf_cache,
                "mkt"          : _mkt_cache,
                "news"         : _news_cache[:25],
                "tweets"       : _tweet_cache[:20],
                "tweet_kw"     : _tweet_keywords,
                "filters"      : {
                    "proximity"   : PROXIMITY_PCT,
                    "min_score"   : MIN_SCORE,
                    "min_wall"    : MIN_WALL_BTC,
                    "tp_pct"      : TP_PCT,
                    "sl_pct"      : SL_PCT,
                    "commission"  : COMMISSION,
                    "fund_strong" : FUND_STRONG,
                    "fund_weak"   : FUND_WEAK,
                    "ls_long"     : LS_CROWD_LONG,
                    "ls_short"    : LS_CROWD_SHORT,
                    "taker_strong": TAKER_STRONG,
                    "vol_mult"    : VOL_MULTIPLIER,
                },
                "pending"      : [{k:v for k,v in s.items() if k!="checks" and k!="entry_candle_idx"}
                                  for s in _pending_signals[-10:]],
                "closed"       : list(reversed(_closed_signals[-20:])),
                "stats"        : stats,
            }
            with _lock:
                _state.update(new_state)

        except Exception as e:
            print(f"[Hata] {e}")
            import traceback; traceback.print_exc()
        time.sleep(REFRESH_SEC)

# ═══════════════════════════════════════════════════════════════
#  HTML
# ═══════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>BTC/USDT — Sinyal Botu</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<!-- Candlestick: native Canvas 2D, harici kütüphane yok -->
<style>
:root{
  --bg:#080c0f;--bg2:#0d1318;--bg3:#111820;--border:#1e2d3a;
  --amber:#f0a500;--amber-dim:#7a5200;
  --green:#00d264;--red:#ff3d5a;
  --text:#c8d8e8;--text-dim:#4a6070;
  --mono:'IBM Plex Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;height:100%;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:9999;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.05) 2px,rgba(0,0,0,.05) 4px)}

/* ── Header ── */
header{display:flex;align-items:center;gap:18px;padding:9px 18px;background:var(--bg2);
  border-bottom:2px solid var(--amber-dim);position:sticky;top:0;z-index:100;flex-wrap:wrap}
.logo{font-size:13px;font-weight:600;letter-spacing:.14em;color:var(--amber);text-transform:uppercase;white-space:nowrap}
.logo span{color:var(--text-dim);font-weight:300}
.hstat{display:flex;flex-direction:column;gap:1px;border-left:1px solid var(--border);padding-left:14px}
.hstat-label{font-size:9px;letter-spacing:.1em;color:var(--text-dim);text-transform:uppercase}
.hstat-val{font-size:13px;font-weight:500}
.price-big{font-size:22px;font-weight:600;color:var(--amber)}
.up{color:var(--green)!important}.down{color:var(--red)!important}.neu{color:var(--text-dim)!important}
.dot-live{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);
  animation:blink 1.4s infinite;margin-left:auto}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.ts-label{font-size:10px;color:var(--text-dim)}

/* ── Layout ── */
.grid{display:grid;grid-template-columns:300px 1fr 310px;
  grid-template-rows:1fr 260px;
  gap:1px;background:var(--border);
  height:calc(100vh - 50px)}

/* ── Alt bar ── */
.bottom-bar{grid-column:1/-1;grid-row:2;display:grid;
  grid-template-columns:1fr 1fr 340px;gap:1px;background:var(--border);
  min-height:0;overflow:hidden}
.bottom-pane{background:var(--bg2);padding:10px 14px;overflow-y:auto;display:flex;flex-direction:column;gap:0}
.bottom-title{font-size:9px;letter-spacing:.15em;text-transform:uppercase;
  color:var(--text-dim);margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.bottom-title::after{content:'';flex:1;height:1px;background:var(--border)}
.panel{background:var(--bg2);padding:12px 14px;overflow:hidden}
.panel-title{font-size:9px;letter-spacing:.15em;text-transform:uppercase;color:var(--text-dim);
  margin-bottom:10px;display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── Order Book ── */
.ob-table{width:100%;border-collapse:collapse}
.ob-table td{padding:3px 3px;font-size:11px;white-space:nowrap;border-bottom:1px solid rgba(255,255,255,.025)}
.bar-bg{height:6px;border-radius:2px;background:var(--border);overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .4s}
.bar-ask{background:var(--red)}.bar-bid{background:var(--green)}
.ob-divider td{padding:5px 3px;font-size:12px;font-weight:600;color:var(--amber);
  background:var(--bg3);border-top:1px solid var(--amber-dim);border-bottom:1px solid var(--amber-dim)}

/* ── Indicator cards ── */
.ind-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border)}
.ind-card{background:var(--bg2);padding:9px 12px;display:flex;flex-direction:column;gap:3px}
.ind-label{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-dim)}
.ind-value{font-size:16px;font-weight:500}
.rsi-bar-bg{height:3px;background:var(--bg3);border-radius:2px;margin-top:4px;position:relative}
.rsi-bar-fill{position:absolute;top:0;left:0;height:100%;border-radius:2px;transition:width .5s}
.rsi-zone-ob{position:absolute;right:35%;top:-2px;bottom:-2px;width:1px;background:var(--red);opacity:.5}
.rsi-zone-os{position:absolute;left:35%;top:-2px;bottom:-2px;width:1px;background:var(--green);opacity:.5}

/* ── Chart ── */
.chart-panel{grid-column:2;grid-row:1;display:flex;flex-direction:column;gap:1px;overflow:hidden}
.chart-wrap{background:var(--bg2);padding:12px 14px 0;flex:1;display:flex;flex-direction:column;overflow:hidden}
#chart-container{flex:1;position:relative;overflow:hidden;min-height:180px}
#price-chart{display:block;position:absolute;inset:0;width:100%;height:100%}

/* ── Win Rate Panel ── */
.wr-panel{background:var(--bg3);border-radius:6px;padding:14px;margin-bottom:10px}
.wr-main{display:flex;align-items:baseline;gap:8px;margin-bottom:10px}
.wr-pct{font-size:36px;font-weight:600}
.wr-label{font-size:10px;color:var(--text-dim)}
.wr-meta{font-size:11px;color:var(--text-dim)}
.wr-bar-wrap{height:6px;background:var(--border);border-radius:3px;margin:8px 0;overflow:hidden}
.wr-bar-fill{height:100%;border-radius:3px;background:var(--green);transition:width .6s}
.wr-split{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.wr-side{background:var(--bg2);border-radius:4px;padding:8px 10px}
.wr-side-label{font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.1em}
.wr-side-val{font-size:15px;font-weight:600;margin-top:2px}
.wr-side-sub{font-size:9px;color:var(--text-dim);margin-top:1px}

/* ── Signals ── */
.signal-box{background:var(--bg3);border-radius:4px;padding:10px 12px;margin-bottom:7px;
  border-left:3px solid var(--border);animation:fadein .3s}
@keyframes fadein{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}
.signal-box.long{border-left-color:var(--green)}.signal-box.short{border-left-color:var(--red)}
.sig-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}
.sig-dir{font-size:12px;font-weight:600}
.sig-score{font-size:10px;color:var(--text-dim)}
.sig-levels{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3px;margin-bottom:6px}
.sig-level{font-size:10px}.sig-level-label{color:var(--text-dim);font-size:9px;text-transform:uppercase}
.checks{display:flex;flex-direction:column;gap:2px}
.check-item{font-size:10px;display:flex;align-items:center;gap:4px}
.stars{letter-spacing:2px;font-size:12px}
.star-fill{color:var(--amber)}.star-empty{color:var(--border)}
.no-signal{color:var(--text-dim);font-size:11px;text-align:center;padding:20px 0;
  border:1px dashed var(--border);border-radius:4px}

/* ── Result table ── */
.result-row{display:grid;grid-template-columns:48px 52px 1fr 40px 62px;
  align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid var(--border);font-size:10px}
.result-row:last-child{border:none}
.badge{font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px}
.badge.win{background:rgba(0,210,100,.15);color:var(--green)}
.badge.loss{background:rgba(255,61,90,.15);color:var(--red)}
.badge.pending{background:rgba(240,165,0,.1);color:var(--amber)}

/* ── Pending section ── */
.pending-row{display:grid;grid-template-columns:48px 52px 1fr 1fr;
  align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid var(--border);font-size:10px}
.pending-row:last-child{border:none}

@media(max-width:1100px){.grid{grid-template-columns:1fr}.chart-panel{grid-column:1;grid-row:auto}}

/* ── Sekmeler (artık kullanılmıyor ama temiz kalsın) ── */
.tab-btn,.tab-pane{display:none}

/* ── Alt bar filtre formu ── */
.fi{display:flex;flex-direction:column;gap:2px}
.fi-label{font-size:9px;color:var(--text-dim);letter-spacing:.06em;text-transform:uppercase}
.fi-input{background:var(--bg3);border:1px solid var(--border);border-radius:3px;
  padding:3px 6px;color:var(--text);font-family:var(--mono);font-size:10px;
  outline:none;width:100%;transition:border-color .15s}
.fi-input:focus{border-color:var(--amber)}

/* ── Haber kartı ── */
.news-item{padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer}
.news-item:last-child{border:none}
.news-item:hover .news-title{color:var(--amber)}
.news-source{font-size:9px;color:var(--text-dim);letter-spacing:.08em;text-transform:uppercase;margin-bottom:2px}
.news-title{font-size:11px;color:var(--text);line-height:1.45;transition:color .15s}
.news-time{font-size:9px;color:var(--text-dim);margin-top:2px}

/* ── Tweet kartı ── */
.tweet-item{padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.tweet-item:last-child{border:none}
.tweet-user{font-size:9px;color:var(--cyan);margin-bottom:2px}
.tweet-text{font-size:11px;color:var(--text);line-height:1.45}
.tweet-time{font-size:9px;color:var(--text-dim);margin-top:2px}
.kw-tag{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;
  background:rgba(240,165,0,.1);border:1px solid var(--amber-dim);border-radius:3px;
  font-size:10px;color:var(--amber);cursor:pointer}
.kw-tag:hover{background:rgba(240,165,0,.2)}
.mkt-card{background:var(--bg2);border-radius:4px;padding:8px 10px}
.mkt-card-label{font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px}
.mkt-card-val{font-size:13px;font-weight:500}
.mkt-card-sub{font-size:10px;color:var(--text-dim);margin-top:2px}

/* ── Karar satırları ── */
.dec-row{display:flex;align-items:center;gap:8px;padding:5px 0;
  border-bottom:1px solid rgba(255,255,255,.03)}
.dec-row:last-child{border:none}
.dec-icon{font-size:13px;width:16px;text-align:center;flex-shrink:0;line-height:1}
.dec-body{flex:1;min-width:0}
.dec-label{font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.08em}
.dec-val{font-size:11px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dec-bar-wrap{width:40px;height:4px;background:var(--border);border-radius:2px;
  flex-shrink:0;position:relative;overflow:visible}
.dec-bar{height:100%;border-radius:2px;transition:width .5s,background .5s}
.dec-pass{color:var(--green)}
.dec-fail{color:var(--red)}
.dec-warn{color:var(--amber)}
.dec-neutral{color:var(--text-dim)}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="logo" id="h-symbol-label">BTC<span>/USDT</span> · SİNYAL BOTU</div>
  <select id="symbol-select" onchange="changeSymbol(this.value)"
    style="background:var(--bg3);color:var(--amber);border:1px solid var(--amber-dim);
           border-radius:4px;padding:4px 8px;font-family:var(--mono);font-size:11px;
           cursor:pointer;outline:none">
    <option value="BTC/USDT">BTC/USDT</option>
    <option value="ETH/USDT">ETH/USDT</option>
    <option value="SOL/USDT">SOL/USDT</option>
    <option value="BNB/USDT">BNB/USDT</option>
    <option value="XRP/USDT">XRP/USDT</option>
    <option value="DOGE/USDT">DOGE/USDT</option>
  </select>
  <div class="hstat"><div class="hstat-label">Fiyat</div><div class="price-big" id="h-price">—</div></div>
  <div class="hstat"><div class="hstat-label">24s Değişim</div><div class="hstat-val" id="h-change">—</div></div>
  <div class="hstat"><div class="hstat-label">RSI (14)</div><div class="hstat-val" id="h-rsi">—</div></div>
  <div class="hstat"><div class="hstat-label">EMA 9/21</div><div class="hstat-val" id="h-ema">—</div></div>
  <div class="hstat"><div class="hstat-label">Tutma Oranı</div><div class="hstat-val" id="h-wr">—</div></div>
  <div class="hstat"><div class="hstat-label">Toplam</div><div class="hstat-val" id="h-total">—</div></div>
  <div class="hstat">
    <div class="hstat-label">1h Trend (HTF)</div>
    <div class="hstat-val" id="h-htf">—</div>
  </div>
  <div class="dot-live" id="dot"></div>
  <div class="ts-label" id="h-ts">--:--:--</div>
</header>

<div class="grid">

  <!-- Sol: Karar Merkezi -->
  <div class="panel" style="grid-row:1;overflow-y:auto">
    <div class="panel-title">Order Book Duvarları</div>
    <table class="ob-table"><tbody id="ob-body">
      <tr><td colspan="3" style="color:var(--text-dim);padding:16px 0;text-align:center">Yükleniyor…</td></tr>
    </tbody></table>

    <div class="panel-title" style="margin-top:14px">5m Teknik Göstergeler</div>
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
        <div class="dec-body"><div class="dec-label">Hacim Spike</div><div class="dec-val" id="dec-vol-val">—</div></div>
        <div class="dec-bar-wrap"><div class="dec-bar" id="dec-vol-bar"></div></div></div>
      <div class="dec-row" id="dec-candle"><div class="dec-icon" id="dec-candle-icon">·</div>
        <div class="dec-body"><div class="dec-label">Mum Formasyonu</div><div class="dec-val" id="dec-candle-val">—</div></div>
        <div class="dec-bar-wrap" style="width:20px"></div></div>
    </div>

    <div class="panel-title" style="margin-top:14px">1h HTF Trend <span id="htf-badge"></span></div>
    <div id="htf-panel" style="background:var(--bg3);border-radius:4px;padding:8px 10px">
      <div style="color:var(--text-dim);font-size:11px">Yükleniyor…</div></div>

    <div class="panel-title" style="margin-top:14px">Piyasa Verisi <span id="mkt-ts" style="color:var(--text-dim);font-size:9px;margin-left:4px"></span></div>
    <div class="dec-row" id="dec-fr"><div class="dec-icon" id="dec-fr-icon">·</div>
      <div class="dec-body"><div class="dec-label">Funding Rate</div><div class="dec-val" id="dec-fr-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-fr-bar" style="width:50%"></div></div></div>
    <div class="dec-row" id="dec-oi"><div class="dec-icon" id="dec-oi-icon">·</div>
      <div class="dec-body"><div class="dec-label">Open Interest</div><div class="dec-val" id="dec-oi-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-oi-bar" style="width:50%"></div></div></div>
    <div class="dec-row" id="dec-ls"><div class="dec-icon" id="dec-ls-icon">·</div>
      <div class="dec-body"><div class="dec-label">Long / Short</div><div class="dec-val" id="dec-ls-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-ls-bar" style="width:50%"></div></div></div>
    <div class="dec-row" id="dec-tk"><div class="dec-icon" id="dec-tk-icon">·</div>
      <div class="dec-body"><div class="dec-label">Taker Buy/Sell</div><div class="dec-val" id="dec-tk-val">—</div></div>
      <div class="dec-bar-wrap"><div class="dec-bar" id="dec-tk-bar" style="width:50%"></div></div></div>

    <div class="panel-title" style="margin-top:14px">Filtre Özeti</div>
    <div id="filter-summary" style="background:var(--bg3);border-radius:4px;padding:8px 10px;font-size:10px;color:var(--text-dim)">
      Sinyal bekleniyor…</div>
  </div>

  <!-- Orta: Grafik -->
  <div class="chart-panel">
    <div class="ind-grid">
      <div class="ind-card">
        <div class="ind-label">RSI (14)</div>
        <div class="ind-value" id="ind-rsi">—</div>
        <div class="rsi-bar-bg"><div class="rsi-bar-fill" id="rsi-fill" style="width:50%;background:var(--amber)"></div>
          <div class="rsi-zone-ob"></div><div class="rsi-zone-os"></div></div>
      </div>
      <div class="ind-card"><div class="ind-label">EMA Trend</div><div class="ind-value" id="ind-ema">—</div></div>
      <div class="ind-card"><div class="ind-label">Hacim Oranı</div><div class="ind-value" id="ind-vol">—</div></div>
    </div>
    <div class="chart-wrap">
      <div class="panel-title">5 Dakikalık Mum Grafiği</div>
      <div id="chart-container">
        <canvas id="price-chart"></canvas>
      </div>
    </div>
  </div>

  <!-- Sağ: Sinyaller -->
  <div class="panel" style="grid-row:1;overflow-y:auto">
    <div class="panel-title">Tutma Oranı</div>
    <div class="wr-panel" id="wr-panel">
      <div class="wr-main">
        <div class="wr-pct" id="wr-pct" style="color:var(--text-dim)">—</div>
        <div><div style="font-size:11px;color:var(--text-dim)">Win Rate</div><div class="wr-meta" id="wr-meta">Sinyal bekleniyor</div></div>
      </div>
      <div class="wr-bar-wrap"><div class="wr-bar-fill" id="wr-bar" style="width:0%"></div></div>
      <div id="wr-comm-info" style="font-size:9px;color:var(--text-dim);margin-top:5px">Komisyon: %0.15 × 2 = %0.30</div>
      <div class="wr-split">
        <div class="wr-side"><div class="wr-side-label">🟢 Long</div>
          <div class="wr-side-val" id="wr-long-rate" style="color:var(--green)">—</div>
          <div class="wr-side-sub" id="wr-long-meta">0 sinyal</div></div>
        <div class="wr-side"><div class="wr-side-label">🔴 Short</div>
          <div class="wr-side-val" id="wr-short-rate" style="color:var(--red)">—</div>
          <div class="wr-side-sub" id="wr-short-meta">0 sinyal</div></div>
      </div>
    </div>

    <div class="panel-title" style="margin-top:14px">Aktif Sinyaller</div>
    <div id="signal-area"><div class="no-signal">⏳ Veri bekleniyor…</div></div>

    <div class="panel-title" style="margin-top:14px">Sonuç Bekleniyor <span id="pending-count" style="color:var(--amber)"></span></div>
    <div id="pending-area"><div style="color:var(--text-dim);font-size:11px;padding:8px 0">Bekleyen sinyal yok</div></div>

    <div class="panel-title" style="margin-top:14px">Kapanmış Sinyaller</div>
    <div id="closed-area"><div style="color:var(--text-dim);font-size:11px;padding:8px 0">Henüz sinyal kapanmadı</div></div>
  </div>

  <!-- Alt Bar: Haberler | Tweetler | Filtre Ayarları -->
  <div class="bottom-bar">

    <!-- Haberler -->
    <div class="bottom-pane">
      <div class="bottom-title">📰 Haberler
        <span style="font-size:9px;color:var(--text-dim);margin-left:2px">5 kaynak</span>
        <button onclick="refreshNews()" style="margin-left:auto;font-size:9px;color:var(--amber);background:none;border:1px solid var(--amber-dim);border-radius:3px;padding:1px 7px;cursor:pointer">↻ Yenile</button>
      </div>
      <div id="news-list" style="overflow-y:auto;flex:1;min-height:0">
        <div style="color:var(--text-dim);font-size:11px;text-align:center;padding:20px 0">Yükleniyor…</div>
      </div>
    </div>

    <!-- Tweetler -->
    <div class="bottom-pane">
      <div class="bottom-title">📈 StockTwits <span style="font-size:9px;color:var(--text-dim)" id="st-sym-label"></span></div>
      <div style="display:flex;gap:5px;margin-bottom:5px;flex-shrink:0;align-items:center">
        <span style="font-size:9px;color:var(--text-dim);white-space:nowrap">Ekstra filtre:</span>
        <input id="kw-input" type="text" placeholder="isteğe bağlı…"
          style="flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:4px;
                 padding:4px 7px;color:var(--text);font-family:var(--mono);font-size:10px;outline:none"
          onkeydown="if(event.key==='Enter') setKeywords()">
        <button onclick="setKeywords()"
          style="background:var(--amber-dim);color:var(--amber);border:none;border-radius:4px;
                 padding:4px 9px;cursor:pointer;font-size:10px;white-space:nowrap">Ara</button>
      </div>
      <div id="kw-tags" style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:4px;flex-shrink:0"></div>
      <div id="tweet-list" style="overflow-y:auto;flex:1;min-height:0">
        <div style="color:var(--text-dim);font-size:11px;text-align:center;padding:20px 0">Kelime girerek aramayı başlatın</div>
      </div>
    </div>

    <!-- Filtre Ayarları -->
    <div class="bottom-pane" style="overflow-y:auto">
      <div class="bottom-title">⚙ Filtre Ayarları
        <button onclick="saveFilters()" style="margin-left:auto;font-size:9px;color:var(--green);background:none;border:1px solid var(--green-dim);border-radius:3px;padding:1px 7px;cursor:pointer">✓ Kaydet</button>
      </div>
      <div id="filter-form" style="display:grid;grid-template-columns:1fr 1fr;gap:3px 8px">
        <div class="fi"><div class="fi-label">Duvar Yakınlık %</div><input class="fi-input" id="fi-proximity" type="number" step="0.01" min="0.1" max="5" placeholder="0.5"></div>
        <div class="fi"><div class="fi-label">Min Skor (1-8)</div><input class="fi-input" id="fi-min_score" type="number" step="1" min="1" max="8" placeholder="2"></div>
        <div class="fi"><div class="fi-label">Min Duvar (BTC)</div><input class="fi-input" id="fi-min_wall" type="number" step="0.5" min="0.5" max="100" placeholder="4"></div>
        <div class="fi"><div class="fi-label">Hacim Çarpanı</div><input class="fi-input" id="fi-vol_mult" type="number" step="0.1" min="1" max="5" placeholder="1.8"></div>
        <div class="fi"><div class="fi-label">TP % (brüt)</div><input class="fi-input" id="fi-tp_pct" type="number" step="0.1" min="0.3" max="10" placeholder="2"></div>
        <div class="fi"><div class="fi-label">SL % (brüt)</div><input class="fi-input" id="fi-sl_pct" type="number" step="0.1" min="0.2" max="5" placeholder="1"></div>
        <div class="fi"><div class="fi-label">Funding Güçlü</div><input class="fi-input" id="fi-fund_strong" type="number" step="0.001" min="0.001" max="0.5" placeholder="0.08"></div>
        <div class="fi"><div class="fi-label">Funding Zayıf</div><input class="fi-input" id="fi-fund_weak" type="number" step="0.001" min="-0.5" max="-0.001" placeholder="-0.08"></div>
        <div class="fi"><div class="fi-label">L/S Kalabalık L</div><input class="fi-input" id="fi-ls_long" type="number" step="0.05" min="1" max="3" placeholder="1.4"></div>
        <div class="fi"><div class="fi-label">L/S Kalabalık S</div><input class="fi-input" id="fi-ls_short" type="number" step="0.05" min="0.1" max="1" placeholder="0.7"></div>
        <div class="fi"><div class="fi-label">Taker Güçlü</div><input class="fi-input" id="fi-taker_strong" type="number" step="0.05" min="1.05" max="3" placeholder="1.25"></div>
        <div class="fi"><div class="fi-label">Komisyon %</div><input class="fi-input" id="fi-commission" type="number" step="0.001" min="0" max="0.5" placeholder="0.15"></div>
      </div>
      <div id="filter-save-msg" style="font-size:9px;color:var(--green);margin-top:5px;min-height:12px"></div>
    </div>

  </div><!-- /bottom-bar -->

</div>
<script>
const PROXIMITY_PCT = __PROXIMITY__;
const TP_PCT        = __TP__;
const SL_PCT        = __SL__;
const COMMISSION    = __COMM__;

// ── Native Canvas Candlestick Chart ─────────────────────────
const cvs = document.getElementById('price-chart');
let prevPrice = null;
let tooltip   = { visible:false, x:0, y:0, candle:null };

function drawChart(candles) {
  if (!candles || candles.length === 0) return;

  const rect = cvs.getBoundingClientRect();
  const W = Math.round(rect.width);
  const H = Math.round(rect.height);

  if (!W || !H || W < 20 || H < 20) {
    setTimeout(() => drawChart(candles), 150);
    return;
  }

  if (cvs.width !== W || cvs.height !== H) {
    cvs.width  = W;
    cvs.height = H;
  }
  const ctx2 = cvs.getContext('2d');
  ctx2.clearRect(0, 0, W, H);
  // c[0]=open c[1]=high c[2]=low c[3]=close c[4]=vol c[5]=ema_fast c[6]=ema_slow c[7]=rsi c[8]=vol_ma
  const data = candles.slice(-60);
  const N = data.length;
  const PAD_L = 8, PAD_R = 72, chartW = W - PAD_L - PAD_R;
  const H_PRICE = Math.floor(H * 0.62);
  const H_VOL   = Math.floor(H * 0.14);
  const H_RSI   = H - H_PRICE - H_VOL;
  const Y_PRICE = 0, Y_VOL = H_PRICE + 2, Y_RSI = H_PRICE + H_VOL + 4;
  const step = chartW / N;
  const candleW = Math.max(1, Math.floor(step) - 2);

  // Price range — sıfır ve NaN filtrele
  const validOpen  = data.map(c => parseFloat(c[0])).filter(v => v > 0 && isFinite(v));
  const validLow   = data.map(c => parseFloat(c[2])).filter(v => v > 0 && isFinite(v));
  const validHigh  = data.map(c => parseFloat(c[1])).filter(v => v > 0 && isFinite(v));
  const emas = data.flatMap(c => [parseFloat(c[5])||0, parseFloat(c[6])||0]).filter(v => v > 0 && isFinite(v));
  if (validLow.length === 0) { setTimeout(()=>drawChart(candles),200); return; }
  let minP = Math.min(...validLow,  ...(emas.length ? emas : [Infinity]));
  let maxP = Math.max(...validHigh, ...(emas.length ? emas : [-Infinity]));
  if (!isFinite(minP)||!isFinite(maxP)||minP>=maxP) { minP=minP-1; maxP=maxP+1; }
  const rng = maxP - minP || 1;
  minP -= rng * 0.04; maxP += rng * 0.06;
  const pRng = maxP - minP;
  const toY = p => Y_PRICE + H_PRICE - ((p - minP) / pRng) * H_PRICE;

  // Grid + price labels
  ctx2.strokeStyle = 'rgba(30,45,58,0.7)'; ctx2.lineWidth = 0.5;
  for (let i = 0; i <= 5; i++) {
    const y = Y_PRICE + (H_PRICE / 5) * i;
    ctx2.beginPath(); ctx2.moveTo(PAD_L, y); ctx2.lineTo(W - PAD_R, y); ctx2.stroke();
    const p = maxP - (pRng / 5) * i;
    ctx2.fillStyle = '#4a6070'; ctx2.font = '10px IBM Plex Mono,monospace'; ctx2.textAlign = 'left';
    ctx2.fillText('$' + p.toLocaleString('en-US',{maximumFractionDigits:0}), W - PAD_R + 5, y + 4);
  }

  // Time labels + vertical grid
  const tStep = Math.max(1, Math.floor(N / 6));
  for (let i = 0; i < N; i += tStep) {
    const x = PAD_L + i * step + step / 2;
    const d = new Date(Date.now() - (N-1-i)*5*60000);
    const label = d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
    ctx2.strokeStyle = 'rgba(30,45,58,0.35)'; ctx2.lineWidth = 0.5;
    ctx2.beginPath(); ctx2.moveTo(x, Y_PRICE); ctx2.lineTo(x, Y_RSI + H_RSI); ctx2.stroke();
    ctx2.fillStyle = '#4a6070'; ctx2.font = '9px IBM Plex Mono,monospace'; ctx2.textAlign = 'center';
    ctx2.fillText(label, x, H - 2);
  }

  // EMA lines — globalAlpha izole et
  ctx2.save();
  ctx2.globalAlpha = 0.75;
  const drawEma = (idx, clr, lw) => {
    ctx2.strokeStyle = clr; ctx2.lineWidth = lw; ctx2.setLineDash([]);
    ctx2.beginPath(); let s = false;
    data.forEach((c, i) => {
      const v = parseFloat(c[idx]); if (!v || v <= 0 || !isFinite(v)) return;
      const x = PAD_L + i * step + step / 2, y = toY(v);
      if (!s) { ctx2.moveTo(x, y); s = true; } else ctx2.lineTo(x, y);
    }); ctx2.stroke();
  };
  drawEma(6, '#f0a500', 1.5);
  drawEma(5, '#00c8e0', 1.5);
  ctx2.restore(); // globalAlpha = 1.0 geri alındı

  // EMA legend
  ctx2.font = '9px IBM Plex Mono,monospace'; ctx2.textAlign = 'left';
  ctx2.fillStyle = '#00c8e0'; ctx2.fillText('EMA' + EMA_FAST, PAD_L + 4, Y_PRICE + 13);
  ctx2.fillStyle = '#f0a500'; ctx2.fillText('EMA' + EMA_SLOW, PAD_L + 44, Y_PRICE + 13);

  // Candles — tamamen izole context
  ctx2.save();
  ctx2.globalAlpha = 1.0;
  ctx2.setLineDash([]);
  data.forEach((c, i) => {
    const open  = parseFloat(c[0]);
    const high  = parseFloat(c[1]);
    const low   = parseFloat(c[2]);
    const close = parseFloat(c[3]);

    if (!open || !high || !low || !close || isNaN(open)) return;

    const x   = PAD_L + i * step + step / 2;
    const col = close >= open ? '#00d264' : '#ff3d5a';

    const yH   = toY(high);
    const yL   = toY(low);
    const yO   = toY(open);
    const yC   = toY(close);
    const bTop = Math.min(yO, yC);
    const bBot = Math.max(yO, yC);

    const xc = Math.round(x);
    const rx  = Math.round(x - candleW / 2);
    const rw  = Math.max(1, candleW | 0);  // bitwise int
    const ry  = Math.round(bTop);
    const rh  = Math.max(1, Math.round(bBot) - ry);

    // Fitil
    ctx2.strokeStyle = col; ctx2.lineWidth = 1;
    ctx2.beginPath();
    ctx2.moveTo(xc, Math.round(yH)); ctx2.lineTo(xc, ry);
    ctx2.moveTo(xc, ry + rh);       ctx2.lineTo(xc, Math.round(yL));
    ctx2.stroke();

    // Gövde
    ctx2.fillStyle = col;
    ctx2.fillRect(rx, ry, rw, rh);
  });
  ctx2.restore();

  // Current price line
  const lc = data[N-1][3], yLast = toY(lc);
  ctx2.setLineDash([3,3]); ctx2.strokeStyle = '#f0a500'; ctx2.lineWidth = 1;
  ctx2.beginPath(); ctx2.moveTo(PAD_L, yLast); ctx2.lineTo(W - PAD_R, yLast); ctx2.stroke();
  ctx2.setLineDash([]);
  ctx2.fillStyle = '#f0a500'; ctx2.fillRect(W - PAD_R + 1, yLast - 8, PAD_R - 3, 16);
  ctx2.fillStyle = '#080c0f'; ctx2.font = '10px IBM Plex Mono,monospace'; ctx2.textAlign = 'center';
  ctx2.fillText('$' + lc.toLocaleString('en-US',{maximumFractionDigits:0}), W - PAD_R + (PAD_R-3)/2 + 1, yLast + 4);

  // Volume panel
  const maxVol = Math.max(...data.map(c => c[4]||0), 1);
  ctx2.fillStyle = 'rgba(30,45,58,0.12)'; ctx2.fillRect(PAD_L, Y_VOL, chartW, H_VOL);
  data.forEach((c, i) => {
    const x = PAD_L + i * step + step / 2, v = c[4]||0;
    const bh = Math.max(1, (v / maxVol) * H_VOL);
    ctx2.fillStyle = c[3] >= c[0] ? 'rgba(0,210,100,0.4)' : 'rgba(255,61,90,0.4)';
    ctx2.fillRect(x - candleW/2, Y_VOL + H_VOL - bh, candleW, bh);
  });
  // Volume MA
  ctx2.strokeStyle = 'rgba(240,165,0,0.55)'; ctx2.lineWidth = 1; ctx2.setLineDash([2,2]);
  ctx2.beginPath(); let vmS = false;
  data.forEach((c, i) => {
    const vm = c[8]; if (!vm) return;
    const x = PAD_L + i * step + step / 2, y = Y_VOL + H_VOL - (vm/maxVol)*H_VOL;
    if (!vmS) { ctx2.moveTo(x, y); vmS = true; } else ctx2.lineTo(x, y);
  }); ctx2.stroke(); ctx2.setLineDash([]);
  ctx2.fillStyle = '#4a6070'; ctx2.font = '8px IBM Plex Mono,monospace'; ctx2.textAlign = 'left';
  ctx2.fillText('VOL', W - PAD_R + 5, Y_VOL + 10);

  // RSI panel
  ctx2.fillStyle = 'rgba(30,45,58,0.12)'; ctx2.fillRect(PAD_L, Y_RSI, chartW, H_RSI);
  const toYR = v => Y_RSI + H_RSI - (v/100)*H_RSI;
  [[70,'rgba(255,61,90,0.25)'],[50,'rgba(100,120,130,0.2)'],[30,'rgba(0,210,100,0.25)']].forEach(([v,c]) => {
    ctx2.strokeStyle = c; ctx2.lineWidth = 0.5;
    ctx2.beginPath(); ctx2.moveTo(PAD_L, toYR(v)); ctx2.lineTo(W - PAD_R, toYR(v)); ctx2.stroke();
  });
  ctx2.fillStyle = 'rgba(255,61,90,0.05)';
  ctx2.fillRect(PAD_L, Y_RSI, chartW, H_RSI*(30/100));
  ctx2.fillStyle = 'rgba(0,210,100,0.05)';
  ctx2.fillRect(PAD_L, toYR(30), chartW, H_RSI*(30/100));
  // RSI line
  ctx2.strokeStyle = '#c070ff'; ctx2.lineWidth = 1.2;
  ctx2.beginPath(); let rS = false;
  data.forEach((c, i) => {
    const rv = c[7]; if (!rv) return;
    const x = PAD_L + i * step + step / 2, y = toYR(rv);
    if (!rS) { ctx2.moveTo(x, y); rS = true; } else ctx2.lineTo(x, y);
  }); ctx2.stroke();
  const lr = data[N-1][7]||0;
  ctx2.fillStyle = '#4a6070'; ctx2.font = '8px IBM Plex Mono,monospace'; ctx2.textAlign = 'left';
  ctx2.fillText('RSI', W - PAD_R + 5, Y_RSI + 10);
  if (lr > 0) {
    ctx2.fillStyle = lr > 70 ? '#ff3d5a' : lr < 30 ? '#00d264' : '#c070ff';
    ctx2.font = '9px IBM Plex Mono,monospace';
    ctx2.fillText(lr.toFixed(1), W - PAD_R + 5, Y_RSI + 22);
  }

  // Tooltip
  if (tooltip.visible && tooltip.candle) {
    const tc = tooltip.candle, tx = Math.min(tooltip.x+12, W-140), ty = Math.max(tooltip.y-95, 4);
    const isBull = tc[3] >= tc[0];
    ctx2.fillStyle = 'rgba(11,17,22,0.96)'; ctx2.strokeStyle = '#1e2d3a'; ctx2.lineWidth = 1;
    ctx2.beginPath(); ctx2.roundRect(tx, ty, 136, 104, 4); ctx2.fill(); ctx2.stroke();
    ctx2.font = '10px IBM Plex Mono,monospace'; ctx2.textAlign = 'left';
    ctx2.fillStyle = isBull ? '#00d264' : '#ff3d5a';
    ctx2.fillText(isBull ? '▲ Yükselen' : '▼ Düşen', tx+8, ty+15);
    ctx2.fillStyle = '#c8d8e8';
    ctx2.fillText('A:'+tc[0].toLocaleString()+'  K:'+tc[3].toLocaleString(), tx+8, ty+29);
    ctx2.fillText('Y:'+tc[1].toLocaleString()+'  D:'+tc[2].toLocaleString(), tx+8, ty+42);
    ctx2.fillStyle = '#00c8e0'; ctx2.fillText('EMA'+EMA_FAST+': $'+(tc[5]||0).toLocaleString(), tx+8, ty+56);
    ctx2.fillStyle = '#f0a500'; ctx2.fillText('EMA'+EMA_SLOW+': $'+(tc[6]||0).toLocaleString(), tx+8, ty+69);
    ctx2.fillStyle = '#c070ff'; ctx2.fillText('RSI: '+((tc[7]||0).toFixed(1)), tx+8, ty+82);
    ctx2.fillStyle = 'rgba(200,216,232,0.55)'; ctx2.fillText('Vol: '+(tc[4]||0).toLocaleString(), tx+8, ty+95);
  }
}

// Mouse hover
cvs.addEventListener('mousemove', e => {
  const candles = window._lastCandles;
  if (!candles) return;
  const data  = candles.slice(-60);
  const N     = data.length;
  const rect  = cvs.getBoundingClientRect();
  const mx    = e.clientX - rect.left;
  const my    = e.clientY - rect.top;
  const PAD_L = 8, PAD_R = 72;
  const step  = (cvs.width - PAD_L - PAD_R) / N;
  const idx   = Math.floor((mx - PAD_L) / step);
  tooltip = (idx >= 0 && idx < N)
    ? { visible:true, x:mx, y:my, candle:data[idx] }
    : { visible:false, x:0, y:0, candle:null };
  drawChart(candles);
});
cvs.addEventListener('mouseleave', () => {
  tooltip = { visible:false, x:0, y:0, candle:null };
  if (window._lastCandles) drawChart(window._lastCandles);
});

// Pencere boyutu değişince yeniden çiz
window.addEventListener('resize', () => {
  if (window._lastCandles) drawChart(window._lastCandles);
});

function initChart(candles) {
  window._lastCandles = candles;
  function tryDraw(attempts) {
    const rect = cvs.getBoundingClientRect();
    if (rect.width > 20 && rect.height > 20) {
      drawChart(candles);
    } else if (attempts < 20) {
      setTimeout(() => tryDraw(attempts + 1), 100);
    }
  }
  tryDraw(0);
}

// ResizeObserver: container boyutlandığında yeniden çiz
const _chartContainer = document.getElementById('chart-container');
if (_chartContainer && window.ResizeObserver) {
  new ResizeObserver(() => {
    if (window._lastCandles) drawChart(window._lastCandles);
  }).observe(_chartContainer);
}

// ── Helpers ─────────────────────────────────────────────────
function stars(n,max=4){
  return `<span class="stars"><span class="star-fill">${'★'.repeat(n)}</span><span class="star-empty">${'☆'.repeat(max-n)}</span></span>`;
}
function fmt(n){return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function checkHtml(ch){
  const icon  = ch.status==='pass'?'✓':'·';
  const color = ch.status==='pass'
    ?(ch.side==='long'?'var(--green)':ch.side==='short'?'var(--red)':'var(--amber)')
    :'var(--text-dim)';
  return `<div class="check-item" style="color:${color}"><span style="width:12px;font-size:9px">${icon}</span><span>${ch.label}</span></div>`;
}

// ── Render: Header ─────────────────────────────────────────
function renderHeader(d) {
  const pe  = document.getElementById('h-price');
  if (!pe) return;
  const dir = prevPrice===null?'neu':d.price>prevPrice?'up':d.price<prevPrice?'down':'neu';
  pe.textContent='$'+fmt(d.price||0); pe.className='price-big '+dir;
  const ce=document.getElementById('h-change');
  if(ce){ ce.textContent=(d.change24h>=0?'+':'')+d.change24h+'%';
          ce.className='hstat-val '+(d.change24h>=0?'up':'down'); }
  const re=document.getElementById('h-rsi');
  if(re){ re.textContent=d.rsi||'—'; re.className='hstat-val '+(d.rsi>65?'down':d.rsi<35?'up':''); }
  const bull=(d.ema_fast||0)>(d.ema_slow||0);
  const emaEl=document.getElementById('h-ema');
  if(emaEl) emaEl.innerHTML=`<span class="${bull?'up':'down'}">${bull?'BULL ▲':'BEAR ▼'}</span>`;

  const st=d.stats||{total:0,wins:0,losses:0,win_rate:0};
  const wrEl=document.getElementById('h-wr');
  if(wrEl){
    if(st.total>0){
      wrEl.textContent=st.win_rate+'%';
      wrEl.style.color=st.win_rate>=55?'var(--green)':st.win_rate>=45?'var(--amber)':'var(--red)';
    } else { wrEl.textContent='—'; wrEl.style.color='var(--text-dim)'; }
  }
  const totEl=document.getElementById('h-total');
  if(totEl) totEl.textContent=st.total>0?`${st.wins}W / ${st.losses}L`:'—';
  const tsEl=document.getElementById('h-ts');
  if(tsEl) tsEl.textContent=d.ts||'';

  const htfEl=document.getElementById('h-htf');
  if(htfEl && d.htf && d.htf.trend){
    const hc=d.htf.trend==='BULL'?'var(--green)':d.htf.trend==='BEAR'?'var(--red)':'var(--amber)';
    const hi=d.htf.trend==='BULL'?'▲':d.htf.trend==='BEAR'?'▼':'─';
    htfEl.innerHTML=`<span style="color:${hc}">${d.htf.trend} ${hi}</span> `
      +`<span style="font-size:10px;color:var(--text-dim)">${d.htf.strength||0}/4</span>`;
  }
  prevPrice=d.price;
}

// ── Render: Indicators ─────────────────────────────────────
function renderIndicators(d) {
  const re=document.getElementById('ind-rsi');
  re.textContent=d.rsi;
  re.style.color=d.rsi>65?'var(--red)':d.rsi<35?'var(--green)':'var(--amber)';
  const fill=document.getElementById('rsi-fill');
  fill.style.width=d.rsi+'%';
  fill.style.background=d.rsi>65?'var(--red)':d.rsi<35?'var(--green)':'var(--amber)';
  const bull=d.ema_fast>d.ema_slow;
  document.getElementById('ind-ema').innerHTML=`<span style="color:${bull?'var(--green)':'var(--red)'}">${bull?'BULL ▲':'BEAR ▼'}</span>`;
  const ve=document.getElementById('ind-vol');
  ve.textContent='×'+d.vol_ratio;
  ve.style.color=d.vol_ratio>=1.8?'var(--amber)':'var(--text)';
}

// ── Render: Order Book ─────────────────────────────────────
function renderOrderBook(d) {
  const price=d.price;
  const maxVol=Math.max(...d.ask_walls.map(w=>w.vol),...d.bid_walls.map(w=>w.vol),1);
  let html='';
  [...d.ask_walls].reverse().forEach(w=>{
    const dist=((w.price-price)/price*100).toFixed(2);
    const bp=(w.vol/maxVol*100).toFixed(1);
    const near=parseFloat(dist)<=PROXIMITY_PCT*100;
    html+=`<tr>
      <td style="color:var(--red)">$${fmt(w.price)}<small style="color:var(--text-dim);margin-left:4px">+${dist}%${near?' ⚡':''}</small></td>
      <td style="color:var(--red);text-align:right">${w.vol.toFixed(1)}</td>
      <td style="padding-left:6px"><div class="bar-bg"><div class="bar-fill bar-ask" style="width:${bp}%"></div></div></td>
    </tr>`;
  });
  html+=`<tr class="ob-divider"><td>▶ $${fmt(price)}</td><td colspan="2" style="color:var(--text-dim);font-size:10px;text-align:right">SPOT</td></tr>`;
  [...d.bid_walls].reverse().forEach(w=>{
    const dist=((price-w.price)/price*100).toFixed(2);
    const bp=(w.vol/maxVol*100).toFixed(1);
    const near=parseFloat(dist)<=PROXIMITY_PCT*100;
    html+=`<tr>
      <td style="color:var(--green)">$${fmt(w.price)}<small style="color:var(--text-dim);margin-left:4px">-${dist}%${near?' ⚡':''}</small></td>
      <td style="color:var(--green);text-align:right">${w.vol.toFixed(1)}</td>
      <td style="padding-left:6px"><div class="bar-bg"><div class="bar-fill bar-bid" style="width:${bp}%"></div></div></td>
    </tr>`;
  });
  document.getElementById('ob-body').innerHTML=html;
}

// ── Render: Win Rate ────────────────────────────────────────
function renderWinRate(d) {
  const st=d.stats;
  const pctEl=document.getElementById('wr-pct');
  const metaEl=document.getElementById('wr-meta');
  const barEl=document.getElementById('wr-bar');

  if(st.total===0){
    pctEl.textContent='—'; pctEl.style.color='var(--text-dim)';
    metaEl.textContent='Sinyal bekleniyor';
    barEl.style.width='0%'; barEl.style.background='var(--green)';
  } else {
    pctEl.textContent=st.win_rate+'%';
    const clr=st.win_rate>=55?'var(--green)':st.win_rate>=45?'var(--amber)':'var(--red)';
    pctEl.style.color=clr;
    const pnlSign = st.net_pnl_pct >= 0 ? '+' : '';
    metaEl.innerHTML=`${st.wins} Kazanç  ${st.losses} Kayıp &nbsp;|&nbsp; 
      Net P&L: <span style="color:${st.net_pnl_pct>=0?'var(--green)':'var(--red)'}">
      ${pnlSign}${st.net_pnl_pct}% / ${pnlSign}$${fmt(st.net_pnl_usd)}</span>`;
    barEl.style.width=st.win_rate+'%'; barEl.style.background=clr;
  }

  // Komisyon bilgisi
  const commInfo = document.getElementById('wr-comm-info');
  if (commInfo) commInfo.textContent = `Komisyon: %${(COMMISSION*100).toFixed(2)} × 2 = %${st.comm_pct} (r/t)`;

  // Long
  const lrEl=document.getElementById('wr-long-rate');
  lrEl.textContent=st.long_total>0?st.long_rate+'%':'—';
  lrEl.style.color=st.long_rate>=55?'var(--green)':st.long_rate>=45?'var(--amber)':'var(--red)';
  document.getElementById('wr-long-meta').textContent=`${st.long_wins}W / ${st.long_total-st.long_wins}L  (${st.long_total})`;

  // Short
  const srEl=document.getElementById('wr-short-rate');
  srEl.textContent=st.short_total>0?st.short_rate+'%':'—';
  srEl.style.color=st.short_rate>=55?'var(--green)':st.short_rate>=45?'var(--amber)':'var(--red)';
  document.getElementById('wr-short-meta').textContent=`${st.short_wins}W / ${st.short_total-st.short_wins}L  (${st.short_total})`;
}

// ── Render: Active Signals ─────────────────────────────────
function renderSignals(d) {
  const area=document.getElementById('signal-area');
  const active   = d.signals.filter(s=>!s.htf_blocked);
  const blocked  = d.signals.filter(s=>s.htf_blocked);

  if(!d.signals.length){
    area.innerHTML='<div class="no-signal">⏳ Sinyal yok — duvar yakınlığı veya teyit skoru yetersiz</div>';
    return;
  }

  let html = '';

  // ── Aktif sinyaller ──
  if (!active.length && blocked.length) {
    const htfTrend = (d.htf&&d.htf.trend)||'?';
    html += `<div class="no-signal" style="border-color:var(--amber-dim)">
      <div style="font-size:14px;margin-bottom:4px">🚫 HTF Filtresi Aktif</div>
      <div style="font-size:10px">1h trend <b style="color:var(--amber)">${htfTrend}</b> — ${blocked.length} sinyal engellendi</div>
    </div>`;
  }

  html += active.map(s=>{
    const isLong=s.dir==='LONG';
    const clr=isLong?'var(--green)':'var(--red)';
    const strength=['Zayıf','Orta','Güçlü','Çok Güçlü'][Math.min(s.score-1,3)];
    const netTp = s.net_tp_pct ?? (TP_PCT-2*COMMISSION)*100;
    const netSl = s.net_sl_pct ?? (SL_PCT+2*COMMISSION)*100;
    const commUsd = s.comm_usd ?? (s.entry*2*COMMISSION);
    const htfBadge = s.htf_trend==='BULL'
      ? `<span style="font-size:9px;background:rgba(0,210,100,.12);color:var(--green);padding:1px 5px;border-radius:3px;margin-left:6px">1h BULL ▲</span>`
      : s.htf_trend==='BEAR'
      ? `<span style="font-size:9px;background:rgba(255,61,90,.12);color:var(--red);padding:1px 5px;border-radius:3px;margin-left:6px">1h BEAR ▼</span>`
      : `<span style="font-size:9px;background:rgba(240,165,0,.1);color:var(--amber);padding:1px 5px;border-radius:3px;margin-left:6px">1h NÖTR</span>`;
    return `<div class="signal-box ${isLong?'long':'short'}">
      <div class="sig-header">
        <span class="sig-dir" style="color:${clr}">${isLong?'🟢 LONG':'🔴 SHORT'}${htfBadge}</span>
        <span class="sig-score">${stars(s.score)} ${strength}</span>
      </div>
      <div class="sig-levels">
        <div class="sig-level"><div class="sig-level-label">Giriş</div><div>$${fmt(s.entry)}</div></div>
        <div class="sig-level">
          <div class="sig-level-label">TP (brüt +${(TP_PCT*100).toFixed(1)}%)</div>
          <div style="color:var(--green)">$${fmt(s.tp)}</div>
        </div>
        <div class="sig-level">
          <div class="sig-level-label">SL (brüt -${(SL_PCT*100).toFixed(1)}%)</div>
          <div style="color:var(--red)">$${fmt(s.sl)}</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:6px;
                  background:rgba(0,0,0,.25);border-radius:3px;padding:5px 7px">
        <div>
          <div style="font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.1em">Net Kar (TP)</div>
          <div style="font-size:12px;font-weight:600;color:var(--green)">
            +${netTp.toFixed(2)}% <span style="font-size:10px;font-weight:400">/ +$${fmt(s.net_tp_usd??0)}</span>
          </div>
        </div>
        <div>
          <div style="font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.1em">Net Zarar (SL)</div>
          <div style="font-size:12px;font-weight:600;color:var(--red)">
            -${netSl.toFixed(2)}% <span style="font-size:10px;font-weight:400">/ -$${fmt(s.net_sl_usd??0)}</span>
          </div>
        </div>
      </div>
      <div style="font-size:9px;color:var(--text-dim);margin-bottom:5px">
        📋 Komisyon: $${fmt(commUsd)} (%${(COMMISSION*2*100).toFixed(2)} r/t) ·
        R/R net: ${(netTp/netSl).toFixed(2)}:1 ·
        Duvar $${fmt(s.wall_price)} · ${s.dist_pct}% uzakta
      </div>
      <div class="checks">${(s.checks||[]).map(checkHtml).join('')}</div>
    </div>`;
  }).join('');

  // ── Engellenen sinyaller (soluk, bilgi amaçlı) ──
  if (blocked.length) {
    html += `<div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;
                text-transform:uppercase;margin:10px 0 6px">
      HTF tarafından engellendi (${blocked.length})
    </div>`;
    html += blocked.map(s=>{
      const isLong=s.dir==='LONG';
      const clr=isLong?'var(--green)':'var(--red)';
      return `<div class="signal-box ${isLong?'long':'short'}"
                   style="opacity:.35;border-left-color:var(--text-dim)">
        <div class="sig-header">
          <span class="sig-dir" style="color:${clr};text-decoration:line-through">
            ${isLong?'LONG':'SHORT'}
          </span>
          <span style="font-size:10px;color:var(--amber)">
            🚫 1h ${s.htf_trend} — engellendi
          </span>
        </div>
        <div style="font-size:10px;color:var(--text-dim)">
          Giriş $${fmt(s.entry)} · Skor ${s.score} · Duvar $${fmt(s.wall_price)}
        </div>
      </div>`;
    }).join('');
  }

  area.innerHTML = html;
}

// ── Render: Pending ────────────────────────────────────────
function renderPending(d) {
  const area=document.getElementById('pending-area');
  document.getElementById('pending-count').textContent=d.pending.length?`(${d.pending.length})`:'';
  if(!d.pending.length){
    area.innerHTML='<div style="color:var(--text-dim);font-size:11px;padding:8px 0">Bekleyen sinyal yok</div>';
    return;
  }
  area.innerHTML=d.pending.map(s=>{
    const isLong=s.dir==='LONG';
    const clr=isLong?'var(--green)':'var(--red)';
    return `<div class="pending-row">
      <span class="badge pending">${s.dir}</span>
      <span style="color:${clr};font-size:11px">$${fmt(s.entry)}</span>
      <span style="color:var(--text-dim);font-size:10px">
        TP <span style="color:var(--green)">$${fmt(s.tp)}</span>
        · SL <span style="color:var(--red)">$${fmt(s.sl)}</span>
      </span>
      <span style="color:var(--text-dim);font-size:10px">${s.ts||''}</span>
    </div>`;
  }).join('');
}

// ── Render: Closed Signals ─────────────────────────────────
function renderClosed(d) {
  const area=document.getElementById('closed-area');
  if(!d.closed.length){
    area.innerHTML='<div style="color:var(--text-dim);font-size:11px;padding:8px 0">Henüz sinyal kapanmadı</div>';
    return;
  }
  area.innerHTML=d.closed.map(s=>{
    const isLong=s.dir==='LONG';
    const isWin=s.outcome==='WIN';
    const pnlPct = s.net_pnl_pct ?? (isWin ? (TP_PCT-2*COMMISSION)*100 : -(SL_PCT+2*COMMISSION)*100);
    const pnlUsd = s.net_pnl_usd ?? 0;
    const pnlSign = pnlPct >= 0 ? '+' : '';
    return `<div class="result-row">
      <span class="badge ${isWin?'win':'loss'}">${isWin?'✓ WIN':'✗ LOSS'}</span>
      <span style="color:${isLong?'var(--green)':'var(--red)'};font-size:10px">${s.dir}</span>
      <span style="font-size:10px;color:${isWin?'var(--green)':'var(--red)'}">
        ${pnlSign}${pnlPct.toFixed(2)}%
      </span>
      <span style="color:var(--amber);font-size:10px">${stars(s.score)}</span>
      <span style="color:var(--text-dim);font-size:9px">${s.close_ts||s.ts||''}</span>
    </div>`;
  }).join('');
}

// ── Render: HTF Panel ──────────────────────────────────────
function renderHTF(d) {
  const panel = document.getElementById('htf-panel');
  if (!d.htf || !d.htf.trend) return;
  const htf = d.htf;
  const clr  = htf.trend==='BULL'?'var(--green)':htf.trend==='BEAR'?'var(--red)':'var(--amber)';
  const icon = htf.trend==='BULL'?'▲':htf.trend==='BEAR'?'▼':'─';
  const barW = (htf.strength/4*100).toFixed(0);

  const detailsHtml = (htf.details||[]).map(d=>{
    const dc = d.side==='bull'?'var(--green)':d.side==='bear'?'var(--red)':'var(--text-dim)';
    const di = d.side==='bull'?'✓':d.side==='bear'?'✗':'·';
    return `<div style="display:flex;gap:6px;align-items:center;font-size:10px;color:${dc};padding:2px 0">
      <span style="width:10px">${di}</span><span>${d.label}</span>
    </div>`;
  }).join('');

  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <span style="font-size:16px;font-weight:600;color:${clr}">${htf.trend} ${icon}</span>
      <span style="font-size:10px;color:var(--text-dim)">
        EMA${HTF_EMA_FAST}: ${fmt(htf.ema_fast)} &nbsp;/&nbsp; EMA${HTF_EMA_SLOW}: ${fmt(htf.ema_slow)}
      </span>
    </div>
    <div style="height:4px;background:var(--border);border-radius:2px;margin-bottom:8px;overflow:hidden">
      <div style="height:100%;width:${barW}%;background:${clr};border-radius:2px;transition:width .5s"></div>
    </div>
    <div>${detailsHtml}</div>
    <div style="font-size:9px;color:var(--text-dim);margin-top:6px">
      1h RSI: ${htf.rsi} &nbsp;·&nbsp; Son güncelleme: ${htf.ts||'—'}
      &nbsp;·&nbsp; ${htf.trend==='NEUTRAL'?'Her iki yön açık':'Yalnız '+htf.trend+' yönde sinyal üretilir'}
    </div>`;
}

// ── Filtre Formu ───────────────────────────────────────────
function renderFilters(filters) {
  if (!filters) return;
  const map = {
    'fi-proximity'   : (filters.proximity * 100).toFixed(2),
    'fi-min_score'   : filters.min_score,
    'fi-min_wall'    : filters.min_wall,
    'fi-vol_mult'    : filters.vol_mult,
    'fi-tp_pct'      : (filters.tp_pct * 100).toFixed(1),
    'fi-sl_pct'      : (filters.sl_pct * 100).toFixed(1),
    'fi-fund_strong' : (filters.fund_strong * 100).toFixed(3),
    'fi-fund_weak'   : (filters.fund_weak * 100).toFixed(3),
    'fi-ls_long'     : filters.ls_long,
    'fi-ls_short'    : filters.ls_short,
    'fi-taker_strong': filters.taker_strong,
    'fi-commission'  : (filters.commission * 100).toFixed(3),
  };
  // Sadece boş inputları doldur (kullanıcı yazmıyorken)
  Object.entries(map).forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el && el !== document.activeElement && !el._dirty) {
      el.placeholder = val;
      if (!el.value) el.value = val;
    }
  });
}

function saveFilters() {
  const raw = {
    proximity   : parseFloat(document.getElementById('fi-proximity')?.value || 0) / 100,
    min_score   : parseInt(document.getElementById('fi-min_score')?.value || 2),
    min_wall    : parseFloat(document.getElementById('fi-min_wall')?.value || 4),
    vol_mult    : parseFloat(document.getElementById('fi-vol_mult')?.value || 1.8),
    tp_pct      : parseFloat(document.getElementById('fi-tp_pct')?.value || 2) / 100,
    sl_pct      : parseFloat(document.getElementById('fi-sl_pct')?.value || 1) / 100,
    fund_strong : parseFloat(document.getElementById('fi-fund_strong')?.value || 0.08) / 100,
    fund_weak   : parseFloat(document.getElementById('fi-fund_weak')?.value || -0.08) / 100,
    ls_long     : parseFloat(document.getElementById('fi-ls_long')?.value || 1.4),
    ls_short    : parseFloat(document.getElementById('fi-ls_short')?.value || 0.7),
    taker_strong: parseFloat(document.getElementById('fi-taker_strong')?.value || 1.25),
    commission  : parseFloat(document.getElementById('fi-commission')?.value || 0.15) / 100,
  };
  fetch('/set_filters', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(raw)
  }).then(r => r.json()).then(d => {
    const msg = document.getElementById('filter-save-msg');
    if (msg) { msg.textContent = '✓ Kaydedildi'; setTimeout(()=>msg.textContent='', 2500); }
  }).catch(e => {
    const msg = document.getElementById('filter-save-msg');
    if (msg) { msg.textContent = '✗ Hata: ' + e; msg.style.color = 'var(--red)'; }
  });
}

// Mark input dirty so renderFilters won't overwrite it
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.fi-input').forEach(el => {
    el.addEventListener('input', () => el._dirty = true);
  });
});

// ── Haber Render ───────────────────────────────────────────
function renderNews(d) {
  const list = document.getElementById('news-list');
  if (!list) return;
  if (!d.news) {
    list.innerHTML = '<div style="color:var(--text-dim);font-size:11px;text-align:center;padding:20px 0">Yükleniyor…</div>';
    return;
  }
  if (d.news.length === 0) {
    list.innerHTML = '<div style="color:var(--text-dim);font-size:11px;text-align:center;padding:20px 0">⚠ RSS feed erişilemedi.<br>İnternet bağlantısını kontrol edin.</div>';
    return;
  }
  list.innerHTML = d.news.map(n => {
    const srcClr = {
      'CoinDesk':'#f0a500','CoinTelegraph':'#00c8e0',
      'Bitcoin Mag':'#ff8c00','Decrypt':'#c070ff','The Block':'#00d264'
    }[n.source] || 'var(--text-dim)';
    const safeUrl = encodeURI(n.url || '');
    return `<div class="news-item" onclick="openLink('${safeUrl}')">
      <div class="news-source" style="color:${srcClr}">${escHtml(n.source)}</div>
      <div class="news-title">${escHtml(n.title)}</div>
      <div class="news-time">${escHtml(n.ts || '')}</div>
    </div>`;
  }).join('');
}

// ── Tweet Render ───────────────────────────────────────────
function renderTweets(d) {
  const list = document.getElementById('tweet-list');
  const tags = document.getElementById('kw-tags');
  if (!list) return;

  // StockTwits symbol label güncelle
  const stLabel = document.getElementById('st-sym-label');
  if (stLabel && d.symbol) {
    const sym = d.symbol.split('/')[0];
    const stSym = {'BTC':'BTC.X','ETH':'ETH.X','SOL':'SOL.X','BNB':'BNB.X',
                   'XRP':'XRP.X','DOGE':'DOGE.X'}[sym] || sym+'.X';
    stLabel.textContent = stSym + ' · 60sn';
  }
  // Keyword tags
  if (tags && Array.isArray(d.tweet_kw) && d.tweet_kw.length > 0) {
    tags.innerHTML = d.tweet_kw.map(k =>
      `<span class="kw-tag" onclick="removeKw('${escHtml(k)}')">${escHtml(k)} <span style="opacity:.5;font-size:9px">✕</span></span>`
    ).join('');
  } else if (tags) {
    tags.innerHTML = '';
  }

  if (!d.tweets || !d.tweets.length) {
    list.innerHTML = '<div style="color:var(--text-dim);font-size:11px;text-align:center;padding:20px 0">StockTwits bekleniyor…</div>';
    return;
  }
  list.innerHTML = d.tweets.map(t =>
    `<div class="tweet-item">
      <div class="tweet-user">${escHtml(t.user || '')}</div>
      <div class="tweet-text">${escHtml(t.text)}</div>
      <div class="tweet-time" style="display:flex;gap:8px;align-items:center">
        <span>${escHtml(t.ts||'')}</span>
        ${t.score !== undefined ? `<span style="color:var(--amber);font-size:9px">▲${t.score}</span>` : ''}
        ${t.comms !== undefined ? `<span style="color:var(--text-dim);font-size:9px">💬${t.comms}</span>` : ''}
        ${t.url ? `<a href="${encodeURI(t.url)}" target="_blank" rel="noopener" style="color:var(--text-dim);font-size:9px;text-decoration:none;margin-left:auto">→ aç</a>` : ''}
      </div>
    </div>`
  ).join('');
}

// ── Yardımcılar ────────────────────────────────────────────
function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function openLink(url) { if(url) window.open(url,'_blank','noopener'); }

function setKeywords() {
  const raw = document.getElementById('kw-input').value.trim();
  if (!raw) return;
  fetch('/set_keywords', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({keywords: raw})
  }).then(r => r.json()).then(d => {
    document.getElementById('kw-input').value = '';
    console.log('[KW]', d.keywords);
  });
}

function removeKw(kw) {
  const tags = document.getElementById('kw-tags');
  const current = Array.from(tags.querySelectorAll('.kw-tag'))
    .map(el => el.textContent.replace('✕','').trim())
    .filter(k => k !== kw);
  fetch('/set_keywords', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({keywords: current.join(', ')})
  });
}

function refreshNews() {
  fetch('/refresh_news', {method:'POST'});
}

// ── Symbol değiştir ────────────────────────────────────────
function changeSymbol(sym) {
  fetch('/change_symbol',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym})});
}

// ── SSE ───────────────────────────────────────────────────
const src=new EventSource('/stream');
src.onmessage=e=>{
  let d;
  try { d=JSON.parse(e.data); } catch(err){ console.error('SSE JSON',err); return; }
  // Symbol sync
  if(d.symbol){
    const sel=document.getElementById('symbol-select');
    if(sel && sel.value!==d.symbol) sel.value=d.symbol;
    const lbl=document.getElementById('h-symbol-label');
    if(lbl){ const p=d.symbol.split('/');
      lbl.innerHTML=p[0]+'<span>/'+( p[1]||'USDT')+'</span> · SİNYAL BOTU'; }
  }
  try{renderHeader(d);}catch(e){console.error('header',e);}
  try{renderIndicators(d);}catch(e){console.error('indicators',e);}
  try{renderOrderBook(d);}catch(e){console.error('orderbook',e);}
  try{renderTech(d);}catch(e){console.error('tech',e);}
  try{renderHTF(d);}catch(e){console.error('htf',e);}
  try{renderMkt(d);}catch(e){console.error('mkt',e);}
  try{renderFilterSummary(d);}catch(e){console.error('filtersummary',e);}
  try{renderFilters(d.filters);}catch(e){console.error('filters',e);}
  try{renderNews(d);}catch(e){console.error('news',e);}
  try{renderTweets(d);}catch(e){console.error('tweets',e);}
  try{renderWinRate(d);}catch(e){console.error('winrate',e);}
  try{renderSignals(d);}catch(e){console.error('signals',e);}
  try{renderPending(d);}catch(e){console.error('pending',e);}
  try{renderClosed(d);}catch(e){console.error('closed',e);}
  try{if(d.candles&&d.candles.length)initChart(d.candles);}catch(e){console.error('chart',e);}
};
src.onerror=()=>{
  const dot=document.getElementById('dot');
  if(dot){dot.style.background='var(--red)';dot.style.boxShadow='0 0 6px var(--red)';}
};
function setDecRow(id, status, valText, barPct, barClr) {
  const row  = document.getElementById('dec-' + id);
  const icon = document.getElementById('dec-' + id + '-icon');
  const val  = document.getElementById('dec-' + id + '-val');
  const bar  = document.getElementById('dec-' + id + '-bar');
  if (!row) return;
  const cfg = {
    pass:    { sym:'✓', cls:'dec-pass',    bg:'var(--green)' },
    fail:    { sym:'✗', cls:'dec-fail',    bg:'var(--red)' },
    warn:    { sym:'·', cls:'dec-warn',    bg:'var(--amber)' },
    neutral: { sym:'─', cls:'dec-neutral', bg:'var(--text-dim)' },
  }[status] || { sym:'·', cls:'dec-neutral', bg:'var(--text-dim)' };
  icon.textContent = cfg.sym;
  icon.className   = 'dec-icon ' + cfg.cls;
  if (val) { val.textContent = valText; val.className = 'dec-val ' + cfg.cls; }
  if (bar) { bar.style.width = (barPct||0) + '%'; bar.style.background = barClr || cfg.bg; }
}

// ── Render: 5m Teknik Göstergeler ──────────────────────────
function renderTech(d) {
  // EMA
  if (d.ema_fast && d.ema_slow) {
    const bull = d.ema_fast > d.ema_slow;
    const diff = ((d.ema_fast - d.ema_slow) / d.ema_slow * 100).toFixed(3);
    setDecRow('ema',
      bull ? 'pass' : 'fail',
      bull ? `${d.ema_fast.toLocaleString()} > ${d.ema_slow.toLocaleString()} (+${Math.abs(diff)}%)`
           : `${d.ema_fast.toLocaleString()} < ${d.ema_slow.toLocaleString()} (${diff}%)`,
      bull ? 75 : 25,
      bull ? 'var(--green)' : 'var(--red)'
    );
  }
  // RSI
  if (d.rsi) {
    const r   = d.rsi;
    const st  = r > 65 ? 'fail' : r < 35 ? 'pass' : 'warn';
    const lbl = r > 65 ? `${r} — aşırı alım` : r < 35 ? `${r} — aşırı satım` : `${r} — nötr`;
    setDecRow('rsi', st, lbl, r, r > 65 ? 'var(--red)' : r < 35 ? 'var(--green)' : 'var(--amber)');
  }
  // Hacim
  if (d.vol_ratio !== undefined) {
    const vr  = d.vol_ratio;
    const st  = vr >= 1.8 ? 'pass' : vr >= 1.2 ? 'warn' : 'neutral';
    const pct = Math.min(100, vr / 3 * 100);
    setDecRow('vol', st, `×${vr} ${vr>=1.8?'— spike!':vr>=1.2?'— yüksek':'— normal'}`,
              pct, vr >= 1.8 ? 'var(--amber)' : 'var(--green)');
  }
  // Mum formasyonu — signals'dan okuyoruz (ilk aktif sinyal)
  const activeSig = (d.signals||[]).find(s => !s.htf_blocked);
  const candleCheck = activeSig && (activeSig.checks||[]).find(c =>
    ['Hammer','Engulfing','Star','Pin'].some(k => c.label.includes(k)));
  if (candleCheck) {
    setDecRow('candle', candleCheck.status==='pass'?'pass':'warn',
              candleCheck.label, 0);
  } else {
    setDecRow('candle', 'neutral', 'Formasyon bekleniyor', 0);
  }
}

// ── Render: Piyasa Verisi (karar satırları) ─────────────────
function renderMkt(d) {
  if (!d.mkt) return;
  const m = d.mkt;
  const ts = document.getElementById('mkt-ts');
  if (ts) ts.textContent = m.ts ? `· ${m.ts}` : '';

  // Funding Rate
  const fr  = m.funding_rate || 0;
  const frP = (fr * 100).toFixed(4);
  const frSt = fr > 0.0008 ? 'fail' : fr < -0.0008 ? 'pass' : 'warn';
  const frLbl = (fr >= 0 ? '+' : '') + frP + '% — ' + (m.funding_str || '');
  const frBar = Math.min(100, Math.abs(fr) / 0.002 * 100);
  setDecRow('fr', frSt, frLbl, frBar,
    fr > 0.0008 ? 'var(--red)' : fr < -0.0008 ? 'var(--green)' : 'var(--amber)');

  // Open Interest
  const oi    = m.oi_change_pct || 0;
  const oiSt  = m.oi_trend === 'artıyor' ? 'pass' : m.oi_trend === 'azalıyor' ? 'fail' : 'warn';
  const oiLbl = (oi >= 0 ? '+' : '') + oi.toFixed(3) + '% — ' + (m.oi_trend || 'nötr');
  const oiBar = Math.min(100, Math.abs(oi) / 0.5 * 100);
  setDecRow('oi', oiSt, oiLbl, oiBar,
    m.oi_trend === 'artıyor' ? 'var(--green)' : m.oi_trend === 'azalıyor' ? 'var(--red)' : 'var(--amber)');

  // L/S Oranı
  const ls    = m.ls_ratio || 1;
  const lsSt  = ls > 1.4 ? 'fail' : ls < 0.7 ? 'pass' : 'warn';
  const lsLbl = ls.toFixed(2) + ' — ' + (m.ls_str || '');
  const lsBar = Math.min(100, (ls / 2) * 100);
  setDecRow('ls', lsSt, lsLbl, lsBar,
    ls > 1.4 ? 'var(--red)' : ls < 0.7 ? 'var(--green)' : 'var(--amber)');

  // Taker
  const tk    = m.taker_ratio || 1;
  const tkSt  = tk > 1.25 ? 'pass' : tk < 0.8 ? 'fail' : 'warn';
  const tkLbl = '×' + tk.toFixed(2) + ' — ' + (m.taker_str || '');
  const tkBar = Math.min(100, (tk / 2) * 100);
  setDecRow('tk', tkSt, tkLbl, tkBar,
    tk > 1.25 ? 'var(--green)' : tk < 0.8 ? 'var(--red)' : 'var(--amber)');
}

// ── Render: Filtre Özeti ────────────────────────────────────
function renderFilterSummary(d) {
  const el = document.getElementById('filter-summary');
  if (!el) return;
  const active  = (d.signals||[]).filter(s => !s.htf_blocked);
  const blocked = (d.signals||[]).filter(s => s.htf_blocked);
  if (!d.signals || !d.signals.length) {
    el.innerHTML = '<span style="color:var(--text-dim)">Duvar yakınlığında sinyal yok</span>';
    return;
  }
  let html = '';
  active.forEach(s => {
    const isLong = s.dir === 'LONG';
    const clr    = isLong ? 'var(--green)' : 'var(--red)';
    const pass   = (s.checks||[]).filter(c => c.status === 'pass').length;
    const fail   = (s.checks||[]).filter(c => c.status === 'fail').length;
    html += `<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;
                          padding:5px 7px;background:rgba(0,0,0,.2);border-radius:3px;
                          border-left:2px solid ${clr}">
      <span style="color:${clr};font-weight:600;font-size:11px">${s.dir}</span>
      <span style="color:var(--text-dim);font-size:10px">Skor ${s.score} · $${s.entry.toLocaleString()}</span>
      <span style="margin-left:auto;font-size:10px">
        <span style="color:var(--green)">✓${pass}</span>
        <span style="color:var(--red);margin-left:4px">✗${fail}</span>
      </span>
    </div>`;
  });
  if (blocked.length) {
    html += `<div style="font-size:9px;color:var(--text-dim);margin-top:4px">
      🚫 ${blocked.length} sinyal engellendi (${blocked.map(s=>s.block_reason||s.htf_trend).join(', ')})
    </div>`;
  }
  el.innerHTML = html || '<span style="color:var(--text-dim)">—</span>';
}

</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════
#  FLASK ROTALAR
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    html = HTML \
        .replace("__PROXIMITY__",    str(PROXIMITY_PCT)) \
        .replace("__TP__",           str(TP_PCT)) \
        .replace("__SL__",           str(SL_PCT)) \
        .replace("__COMM__",         str(COMMISSION)) \
        .replace("__HTF_EMA_FAST__", str(HTF_EMA_FAST)) \
        .replace("__HTF_EMA_SLOW__", str(HTF_EMA_SLOW)) \
        .replace("__EMA_FAST__",     str(EMA_FAST)) \
        .replace("__EMA_SLOW__",     str(EMA_SLOW))
    return render_template_string(html)

@app.route("/stream")
def stream():
    def event_stream():
        last_ts=None
        while True:
            with _lock:
                ts=_state.get("ts"); state=dict(_state)
            if ts and ts!=last_ts:
                yield f"data: {json.dumps(state)}\n\n"
                last_ts=ts
            time.sleep(1)
    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/set_keywords", methods=["POST"])
def set_keywords():
    """Tweet anahtar kelimelerini güncelle ve hemen fetch yap."""
    global _tweet_keywords, _tweet_cache, _tweet_last_fetch
    data = flask_request.get_json(silent=True) or {}
    raw  = data.get("keywords", "")
    kws  = [k.strip() for k in re.split(r"[,\s]+", raw) if k.strip()][:6]
    _tweet_keywords   = kws
    _tweet_last_fetch = 0   # sıfırla → hemen çekilsin
    return {"ok": True, "keywords": kws}

@app.route("/change_symbol", methods=["POST"])
def change_symbol():
    global SYMBOL, _tweet_keywords, _tweet_last_fetch, _news_last_fetch
    data = flask_request.get_json(silent=True) or {}
    sym  = data.get("symbol","").strip().upper()
    if "/" not in sym: sym = sym + "/USDT"
    SYMBOL            = sym
    _tweet_keywords   = [sym.split("/")[0]]  # ETH/USDT → ["ETH"]
    _tweet_last_fetch = 0                    # hemen yenile
    _news_last_fetch  = 0                    # haberleri de yenile
    return {"ok": True, "symbol": SYMBOL}

@app.route("/refresh_news", methods=["POST"])
def refresh_news():
    global _news_cache, _news_last_fetch
    _news_last_fetch = 0
    return {"ok": True}

@app.route("/set_filters", methods=["POST"])
def set_filters():
    """Dashboard'dan filtre parametrelerini canlı güncelle."""
    global PROXIMITY_PCT, MIN_SCORE, MIN_WALL_BTC
    global TP_PCT, SL_PCT, COMMISSION
    global FUND_STRONG, FUND_WEAK, LS_CROWD_LONG, LS_CROWD_SHORT, TAKER_STRONG
    global OI_CHANGE_THR, VOL_MULTIPLIER
    data = flask_request.get_json(silent=True) or {}
    changed = []
    def _f(key, var_ref, lo, hi):
        if key in data:
            try:
                v = float(data[key])
                if lo <= v <= hi:
                    return round(v, 6)
            except Exception:
                pass
        return var_ref
    PROXIMITY_PCT   = _f("proximity",   PROXIMITY_PCT,   0.001, 0.05)
    MIN_SCORE       = max(1, min(8, int(data.get("min_score",   MIN_SCORE))))
    MIN_WALL_BTC    = _f("min_wall",     MIN_WALL_BTC,    0.5,  100)
    TP_PCT          = _f("tp_pct",       TP_PCT,          0.003, 0.10)
    SL_PCT          = _f("sl_pct",       SL_PCT,          0.002, 0.05)
    COMMISSION      = _f("commission",   COMMISSION,      0.0,   0.005)
    FUND_STRONG     = _f("fund_strong",  FUND_STRONG,     0.0001, 0.005)
    FUND_WEAK       = _f("fund_weak",    FUND_WEAK,      -0.005, -0.0001)
    LS_CROWD_LONG   = _f("ls_long",      LS_CROWD_LONG,   1.0,  3.0)
    LS_CROWD_SHORT  = _f("ls_short",     LS_CROWD_SHORT,  0.1,  1.0)
    TAKER_STRONG    = _f("taker_strong", TAKER_STRONG,    1.05, 3.0)
    VOL_MULTIPLIER  = _f("vol_mult",     VOL_MULTIPLIER,  1.0,  5.0)
    return {"ok": True, "filters": {
        "proximity": PROXIMITY_PCT, "min_score": MIN_SCORE,
        "min_wall": MIN_WALL_BTC,   "tp_pct": TP_PCT, "sl_pct": SL_PCT,
        "commission": COMMISSION,
        "fund_strong": FUND_STRONG, "fund_weak": FUND_WEAK,
        "ls_long": LS_CROWD_LONG,   "ls_short": LS_CROWD_SHORT,
        "taker_strong": TAKER_STRONG, "vol_mult": VOL_MULTIPLIER,
    }}

# ═══════════════════════════════════════════════════════════════
#  BAŞLAT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    print("\n✅  Dashboard hazır → http://localhost:5000\n")
    app.run(debug=False, port=5000, threaded=True)
