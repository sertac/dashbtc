"""
BTC/USDT Sinyal Botu — Flask Dashboard (Tek Dosya)
===================================================
Yeni: Sinyal sonuç takibi (TP/SL) + Tutma oranı ekranı
Kurulum : pip install flask ccxt pandas ta
Çalıştır: python btc_dashboard.py → http://localhost:5000
"""

import json, time, threading
from datetime import datetime
from flask import Flask, Response, render_template_string
import ccxt, pandas as pd, ta

# ═══════════════════════════════════════════════════════════════
#  AYARLAR
# ═══════════════════════════════════════════════════════════════
SYMBOL         = "ETH/USDT"
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
# Sinyal sonucu için bekleme süresi (5dk mumlar, max 20 mum = 100 dk)
MAX_CANDLES_WAIT = 20

app      = Flask(__name__)
exchange = ccxt.binance({"options": {"defaultType": "future"}})

_lock            = threading.Lock()
_state           = {}
_pending_signals = []   # Sonucu beklenen sinyaller
_closed_signals  = []   # Sonuçlanmış sinyaller (TP/SL)
_htf_cache       = {"trend": "NEUTRAL", "ema_fast": 0, "ema_slow": 0,
                    "rsi": 50, "score": 0, "details": [], "ts": None}
_htf_last_fetch  = 0    # Son HTF fetch unix timestamp

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
    htf     = _htf_cache   # global HTF durumu

    for wp,wv in bid_walls:
        if wp>=price: continue
        dist=(price-wp)/price
        if dist>PROXIMITY_PCT: continue

        # HTF filtresi: üst trend BEAR ise LONG sinyali engelle
        htf_ok   = htf["trend"] != "BEAR"
        htf_boost = htf["trend"] == "BULL"   # aynı yöndeyse bonus puan

        sc,ch=score_reversal(df,"LONG")

        # HTF bilgisini teyit listesine ekle
        if htf["trend"] == "BULL":
            ch.append({"label": f"1h trend BULL ✓ (güç {htf['strength']}/4)", "status":"pass","side":"long"})
            sc += 1   # HTF hizalaması → +1 bonus puan
        elif htf["trend"] == "NEUTRAL":
            ch.append({"label": f"1h trend NÖTR (güç {htf['strength']}/4)", "status":"warn","side":"neutral"})
        else:  # BEAR
            ch.append({"label": f"1h trend BEAR — LONG engellendi", "status":"fail","side":"short"})

        if not htf_ok:
            # Engellendi — sinyali filtered listesine ekle (ekranda göstermek için)
            signals.append({"dir":"LONG","entry":price,
                "tp":round(price*(1+TP_PCT),2),"sl":round(price*(1-SL_PCT),2),
                "net_tp_pct":round((TP_PCT-2*COMMISSION)*100,2),
                "net_sl_pct":round((SL_PCT+2*COMMISSION)*100,2),
                "net_tp_usd":round(price*(TP_PCT-2*COMMISSION),2),
                "net_sl_usd":round(price*(SL_PCT+2*COMMISSION),2),
                "comm_usd":round(price*2*COMMISSION,2),
                "wall_price":wp,"wall_vol":round(wv,2),
                "dist_pct":round(dist*100,3),"score":sc,"checks":ch,
                "htf_blocked": True, "htf_trend": htf["trend"]})
            continue

        if sc >= MIN_SCORE:
            signals.append({"dir":"LONG","entry":price,
                "tp":round(price*(1+TP_PCT),2),"sl":round(price*(1-SL_PCT),2),
                "net_tp_pct":round((TP_PCT-2*COMMISSION)*100,2),
                "net_sl_pct":round((SL_PCT+2*COMMISSION)*100,2),
                "net_tp_usd":round(price*(TP_PCT-2*COMMISSION),2),
                "net_sl_usd":round(price*(SL_PCT+2*COMMISSION),2),
                "comm_usd":round(price*2*COMMISSION,2),
                "wall_price":wp,"wall_vol":round(wv,2),
                "dist_pct":round(dist*100,3),"score":sc,"checks":ch,
                "htf_blocked": False, "htf_trend": htf["trend"]})

    for wp,wv in ask_walls:
        if wp<=price: continue
        dist=(wp-price)/price
        if dist>PROXIMITY_PCT: continue

        # HTF filtresi: üst trend BULL ise SHORT sinyali engelle
        htf_ok    = htf["trend"] != "BULL"
        htf_boost = htf["trend"] == "BEAR"

        sc,ch=score_reversal(df,"SHORT")

        if htf["trend"] == "BEAR":
            ch.append({"label": f"1h trend BEAR ✓ (güç {htf['strength']}/4)", "status":"pass","side":"short"})
            sc += 1
        elif htf["trend"] == "NEUTRAL":
            ch.append({"label": f"1h trend NÖTR (güç {htf['strength']}/4)", "status":"warn","side":"neutral"})
        else:  # BULL
            ch.append({"label": f"1h trend BULL — SHORT engellendi", "status":"fail","side":"long"})

        if not htf_ok:
            signals.append({"dir":"SHORT","entry":price,
                "tp":round(price*(1-TP_PCT),2),"sl":round(price*(1+SL_PCT),2),
                "net_tp_pct":round((TP_PCT-2*COMMISSION)*100,2),
                "net_sl_pct":round((SL_PCT+2*COMMISSION)*100,2),
                "net_tp_usd":round(price*(TP_PCT-2*COMMISSION),2),
                "net_sl_usd":round(price*(SL_PCT+2*COMMISSION),2),
                "comm_usd":round(price*2*COMMISSION,2),
                "wall_price":wp,"wall_vol":round(wv,2),
                "dist_pct":round(dist*100,3),"score":sc,"checks":ch,
                "htf_blocked": True, "htf_trend": htf["trend"]})
            continue

        if sc >= MIN_SCORE:
            signals.append({"dir":"SHORT","entry":price,
                "tp":round(price*(1-TP_PCT),2),"sl":round(price*(1+SL_PCT),2),
                "net_tp_pct":round((TP_PCT-2*COMMISSION)*100,2),
                "net_sl_pct":round((SL_PCT+2*COMMISSION)*100,2),
                "net_tp_usd":round(price*(TP_PCT-2*COMMISSION),2),
                "net_sl_usd":round(price*(SL_PCT+2*COMMISSION),2),
                "comm_usd":round(price*2*COMMISSION,2),
                "wall_price":wp,"wall_vol":round(wv,2),
                "dist_pct":round(dist*100,3),"score":sc,"checks":ch,
                "htf_blocked": False, "htf_trend": htf["trend"]})

    signals.sort(key=lambda x: (x.get("htf_blocked",False), -x["score"]))
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
    global _pending_signals, _htf_cache, _htf_last_fetch
    while True:
        try:
            # ── HTF: her HTF_REFRESH saniyede bir güncelle ──────────
            now = time.time()
            if now - _htf_last_fetch >= HTF_REFRESH:
                try:
                    df_htf       = fetch_htf_ohlcv()
                    _htf_cache   = calc_htf_trend(df_htf)
                    _htf_last_fetch = now
                    print(f"[HTF] 1h trend: {_htf_cache['trend']}  "
                          f"(bull={_htf_cache['bull_sc']} bear={_htf_cache['bear_sc']})  "
                          f"RSI={_htf_cache['rsi']}")
                except Exception as e:
                    print(f"[HTF Hata] {e}")

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
            candles   = df.tail(60)[["open","high","low","close","volume"]].round(2).values.tolist()

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
.grid{display:grid;grid-template-columns:300px 1fr 310px;gap:1px;background:var(--border);
  min-height:calc(100vh - 50px)}
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
.chart-panel{grid-column:2;grid-row:1/3;display:flex;flex-direction:column;gap:1px}
.chart-wrap{background:var(--bg2);padding:12px 14px;flex:1;display:flex;flex-direction:column;min-height:300px;height:0}
canvas{flex:1}

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
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="logo">BTC<span>/USDT</span> · SİNYAL BOTU</div>
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

  <!-- Sol: Order Book -->
  <div class="panel" style="grid-row:1/3;overflow-y:auto;max-height:calc(100vh - 50px)">
    <div class="panel-title">Order Book Duvarları</div>
    <table class="ob-table"><tbody id="ob-body">
      <tr><td colspan="3" style="color:var(--text-dim);padding:20px 0;text-align:center">Yükleniyor…</td></tr>
    </tbody></table>

    <!-- HTF Trend Detay -->
    <div class="panel-title" style="margin-top:16px">1h Trend Analizi</div>
    <div id="htf-panel" style="background:var(--bg3);border-radius:4px;padding:10px 12px">
      <div style="color:var(--text-dim);font-size:11px">Yükleniyor…</div>
    </div>
  </div>

  <!-- Orta -->
  <div class="chart-panel">
    <div class="ind-grid">
      <div class="ind-card">
        <div class="ind-label">RSI (14)</div>
        <div class="ind-value" id="ind-rsi">—</div>
        <div class="rsi-bar-bg">
          <div class="rsi-bar-fill" id="rsi-fill" style="width:50%;background:var(--amber)"></div>
          <div class="rsi-zone-ob"></div><div class="rsi-zone-os"></div>
        </div>
      </div>
      <div class="ind-card">
        <div class="ind-label">EMA Trend</div>
        <div class="ind-value" id="ind-ema">—</div>
      </div>
      <div class="ind-card">
        <div class="ind-label">Hacim Oranı</div>
        <div class="ind-value" id="ind-vol">—</div>
      </div>
    </div>
    <div class="chart-wrap">
      <div class="panel-title">5 Dakikalık Mum Grafiği</div>
      <canvas id="price-chart" style="display:block;width:100%;height:100%;cursor:crosshair"></canvas>
    </div>
  </div>

  <!-- Sağ: Win Rate + Sinyaller + Geçmiş -->
  <div class="panel" style="grid-row:1/3;overflow-y:auto;max-height:calc(100vh - 50px)">

    <!-- Tutma Oranı -->
    <div class="panel-title">Tutma Oranı</div>
    <div class="wr-panel" id="wr-panel">
      <div class="wr-main">
        <div class="wr-pct" id="wr-pct" style="color:var(--text-dim)">—</div>
        <div>
          <div style="font-size:11px;color:var(--text-dim)">Win Rate</div>
          <div class="wr-meta" id="wr-meta">Sinyal bekleniyor</div>
        </div>
      </div>
      <div class="wr-bar-wrap"><div class="wr-bar-fill" id="wr-bar" style="width:0%"></div></div>
      <div id="wr-comm-info" style="font-size:9px;color:var(--text-dim);margin-top:5px;letter-spacing:.05em">
        Komisyon: %0.15 × 2 = %0.30 (round-trip)
      </div>
      <div class="wr-split">
        <div class="wr-side">
          <div class="wr-side-label">🟢 Long</div>
          <div class="wr-side-val" id="wr-long-rate" style="color:var(--green)">—</div>
          <div class="wr-side-sub" id="wr-long-meta">0 sinyal</div>
        </div>
        <div class="wr-side">
          <div class="wr-side-label">🔴 Short</div>
          <div class="wr-side-val" id="wr-short-rate" style="color:var(--red)">—</div>
          <div class="wr-side-sub" id="wr-short-meta">0 sinyal</div>
        </div>
      </div>
    </div>

    <!-- Aktif Sinyaller -->
    <div class="panel-title">Aktif Sinyaller</div>
    <div id="signal-area">
      <div class="no-signal">⏳ Veri bekleniyor…</div>
    </div>

    <!-- Bekleyen (Sonuç Bekleniyor) -->
    <div class="panel-title" style="margin-top:14px">Sonuç Bekleniyor <span id="pending-count" style="color:var(--amber)"></span></div>
    <div id="pending-area">
      <div style="color:var(--text-dim);font-size:11px;padding:8px 0">Bekleyen sinyal yok</div>
    </div>

    <!-- Kapalı Sinyaller -->
    <div class="panel-title" style="margin-top:14px">Kapanmış Sinyaller</div>
    <div id="closed-area">
      <div style="color:var(--text-dim);font-size:11px;padding:8px 0">Henüz sinyal kapanmadı</div>
    </div>

  </div>
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

  // Gerçek piksel boyutlarını al
  const rect = cvs.getBoundingClientRect();
  const W = Math.floor(rect.width)  || cvs.parentElement.clientWidth  - 28;
  const H = Math.floor(rect.height) || 300;
  cvs.width  = W;
  cvs.height = H;
  const ctx2 = cvs.getContext('2d');
  ctx2.clearRect(0, 0, W, H);

  // Padding
  const PAD_L = 8, PAD_R = 72, PAD_T = 12, PAD_B = 28;
  const chartW = W - PAD_L - PAD_R;
  const chartH = H - PAD_T - PAD_B;

  // Son 60 mum
  const data = candles.slice(-60);
  const N    = data.length;
  const lows  = data.map(c => c[2]);
  const highs = data.map(c => c[1]);
  let minP = Math.min(...lows);
  let maxP = Math.max(...highs);
  const range = maxP - minP || 1;
  // Biraz nefes payı
  minP -= range * 0.05;
  maxP += range * 0.05;
  const priceRange = maxP - minP;

  const toY = p => PAD_T + chartH - ((p - minP) / priceRange) * chartH;

  const candleW = Math.max(2, Math.floor(chartW / N) - 2);
  const step    = chartW / N;

  // ── Arka plan ızgarası ────────────────────────────
  ctx2.strokeStyle = 'rgba(30,45,58,0.8)';
  ctx2.lineWidth   = 1;
  const gridLines  = 6;
  for (let i = 0; i <= gridLines; i++) {
    const y    = PAD_T + (chartH / gridLines) * i;
    const price = maxP - (priceRange / gridLines) * i;
    ctx2.beginPath();
    ctx2.moveTo(PAD_L, y);
    ctx2.lineTo(W - PAD_R, y);
    ctx2.stroke();
    // Fiyat etiketi
    ctx2.fillStyle = '#4a6070';
    ctx2.font      = '10px IBM Plex Mono, monospace';
    ctx2.textAlign = 'left';
    ctx2.fillText('$' + price.toLocaleString('en-US', {maximumFractionDigits: 0}),
                  W - PAD_R + 6, y + 4);
  }

  // Dikey ızgara + zaman etiketleri
  const timeStep = Math.floor(N / 6);
  for (let i = 0; i < N; i += timeStep) {
    const x = PAD_L + i * step + step / 2;
    ctx2.strokeStyle = 'rgba(30,45,58,0.5)';
    ctx2.beginPath(); ctx2.moveTo(x, PAD_T); ctx2.lineTo(x, PAD_T + chartH); ctx2.stroke();
    // Zaman etiketi (N-i mum önce = kaç dakika önce)
    const minsAgo = (N - 1 - i) * 5;
    const d       = new Date(Date.now() - minsAgo * 60000);
    const label   = d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
    ctx2.fillStyle = '#4a6070';
    ctx2.font      = '10px IBM Plex Mono, monospace';
    ctx2.textAlign = 'center';
    ctx2.fillText(label, x, PAD_T + chartH + 18);
  }

  // ── Mumlar ────────────────────────────────────────
  data.forEach((c, i) => {
    const [open, high, low, close] = c;
    const x     = PAD_L + i * step + step / 2;
    const isBull = close >= open;
    const color  = isBull ? '#00d264' : '#ff3d5a';

    const yOpen  = toY(open);
    const yClose = toY(close);
    const yHigh  = toY(high);
    const yLow   = toY(low);
    const bodyTop    = Math.min(yOpen, yClose);
    const bodyBottom = Math.max(yOpen, yClose);
    const bodyH      = Math.max(1, bodyBottom - bodyTop);

    // Fitil
    ctx2.strokeStyle = color;
    ctx2.lineWidth   = 1;
    ctx2.beginPath();
    ctx2.moveTo(x, yHigh);
    ctx2.lineTo(x, bodyTop);
    ctx2.moveTo(x, bodyBottom);
    ctx2.lineTo(x, yLow);
    ctx2.stroke();

    // Gövde
    ctx2.fillStyle = isBull ? color : color;
    if (isBull) {
      ctx2.fillStyle = color;
    } else {
      ctx2.fillStyle = color;
    }
    ctx2.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);

    // Doji veya çok küçük gövde → çizgi
    if (bodyH <= 1) {
      ctx2.strokeStyle = color;
      ctx2.lineWidth   = 1;
      ctx2.beginPath();
      ctx2.moveTo(x - candleW / 2, yOpen);
      ctx2.lineTo(x + candleW / 2, yOpen);
      ctx2.stroke();
    }
  });

  // ── Mevcut fiyat çizgisi ──────────────────────────
  const lastClose = data[data.length - 1][3];
  const yLast     = toY(lastClose);
  ctx2.setLineDash([4, 4]);
  ctx2.strokeStyle = '#f0a500';
  ctx2.lineWidth   = 1;
  ctx2.beginPath();
  ctx2.moveTo(PAD_L, yLast);
  ctx2.lineTo(W - PAD_R, yLast);
  ctx2.stroke();
  ctx2.setLineDash([]);
  // Fiyat kutusu
  ctx2.fillStyle = '#f0a500';
  ctx2.fillRect(W - PAD_R + 2, yLast - 9, PAD_R - 4, 18);
  ctx2.fillStyle = '#080c0f';
  ctx2.font      = '10px IBM Plex Mono, monospace';
  ctx2.textAlign = 'center';
  ctx2.fillText('$' + lastClose.toLocaleString('en-US', {maximumFractionDigits: 0}),
                W - PAD_R + (PAD_R - 4) / 2 + 2, yLast + 4);

  // ── Tooltip (mouse hover) ─────────────────────────
  if (tooltip.visible && tooltip.candle) {
    const tc = tooltip.candle;
    const tx = Math.min(tooltip.x + 12, W - 130);
    const ty = Math.max(tooltip.y - 70, PAD_T);
    ctx2.fillStyle = 'rgba(13,19,24,0.95)';
    ctx2.strokeStyle = '#1e2d3a';
    ctx2.lineWidth = 1;
    ctx2.beginPath();
    ctx2.roundRect(tx, ty, 118, 76, 4);
    ctx2.fill(); ctx2.stroke();
    ctx2.fillStyle = '#c8d8e8';
    ctx2.font      = '10px IBM Plex Mono, monospace';
    ctx2.textAlign = 'left';
    const isBull = tc[3] >= tc[0];
    ctx2.fillStyle = isBull ? '#00d264' : '#ff3d5a';
    ctx2.fillText(isBull ? '▲ Yükselen' : '▼ Düşen', tx + 8, ty + 16);
    ctx2.fillStyle = '#c8d8e8';
    ctx2.fillText(`A: $${tc[0].toLocaleString()}`, tx + 8, ty + 30);
    ctx2.fillText(`Y: $${tc[1].toLocaleString()}`, tx + 8, ty + 43);
    ctx2.fillText(`D: $${tc[2].toLocaleString()}`, tx + 8, ty + 56);
    ctx2.fillStyle = isBull ? '#00d264' : '#ff3d5a';
    ctx2.fillText(`K: $${tc[3].toLocaleString()}`, tx + 8, ty + 69);
  }
}

// Mouse hover
cvs.addEventListener('mousemove', e => {
  const candles = window._lastCandles;
  if (!candles) return;
  const data = candles.slice(-60);
  const N    = data.length;
  const rect = cvs.getBoundingClientRect();
  const mx   = e.clientX - rect.left;
  const PAD_L = 8, PAD_R = 72;
  const step  = (cvs.width - PAD_L - PAD_R) / N;
  const idx   = Math.floor((mx - PAD_L) / step);
  if (idx >= 0 && idx < N) {
    tooltip = { visible:true, x:mx, y:e.clientY - rect.top, candle:data[idx] };
  } else {
    tooltip = { visible:false, x:0, y:0, candle:null };
  }
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
  drawChart(candles);
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
  const dir = prevPrice===null?'neu':d.price>prevPrice?'up':d.price<prevPrice?'down':'neu';
  pe.textContent='$'+fmt(d.price); pe.className='price-big '+dir;
  const ce=document.getElementById('h-change');
  ce.textContent=(d.change24h>=0?'+':'')+d.change24h+'%';
  ce.className='hstat-val '+(d.change24h>=0?'up':'down');
  const re=document.getElementById('h-rsi');
  re.textContent=d.rsi; re.className='hstat-val '+(d.rsi>65?'down':d.rsi<35?'up':'');
  const bull=d.ema_fast>d.ema_slow;
  document.getElementById('h-ema').innerHTML=`<span class="${bull?'up':'down'}">${bull?'BULL ▲':'BEAR ▼'}</span>`;

  // Win rate header
  const st=d.stats;
  const wrEl=document.getElementById('h-wr');
  if(st.total>0){
    wrEl.textContent=st.win_rate+'%';
    wrEl.style.color=st.win_rate>=55?'var(--green)':st.win_rate>=45?'var(--amber)':'var(--red)';
  } else { wrEl.textContent='—'; wrEl.style.color='var(--text-dim)'; }
  document.getElementById('h-total').textContent = st.total>0 ? `${st.wins}W / ${st.losses}L` : '—';
  document.getElementById('h-ts').textContent=d.ts;

  // HTF badge
  const htfEl=document.getElementById('h-htf');
  if(d.htf && d.htf.trend){
    const htf=d.htf;
    const hc=htf.trend==='BULL'?'var(--green)':htf.trend==='BEAR'?'var(--red)':'var(--amber)';
    const hi=htf.trend==='BULL'?'▲':htf.trend==='BEAR'?'▼':'─';
    htfEl.innerHTML=`<span style="color:${hc}">${htf.trend} ${hi}</span> `
      +`<span style="font-size:10px;color:var(--text-dim)">${htf.strength}/4</span>`;
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

// ── SSE ───────────────────────────────────────────────────
const HTF_EMA_FAST = __HTF_EMA_FAST__;
const HTF_EMA_SLOW = __HTF_EMA_SLOW__;

const src=new EventSource('/stream');
src.onmessage=e=>{
  const d=JSON.parse(e.data);
  renderHeader(d);
  renderIndicators(d);
  renderOrderBook(d);
  renderHTF(d);
  renderWinRate(d);
  renderSignals(d);
  renderPending(d);
  renderClosed(d);
  if(d.candles&&d.candles.length) initChart(d.candles);
};
src.onerror=()=>{
  const dot=document.getElementById('dot');
  dot.style.background='var(--red)';dot.style.boxShadow='0 0 6px var(--red)';
};
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
        .replace("__HTF_EMA_SLOW__", str(HTF_EMA_SLOW))
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

# ═══════════════════════════════════════════════════════════════
#  BAŞLAT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    print("\n✅  Dashboard hazır → http://localhost:5000\n")
    app.run(debug=False, port=5000, threaded=True)
