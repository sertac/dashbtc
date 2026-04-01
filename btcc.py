"""
BTC/USDT Gelişmiş Sinyal Botu — v2
====================================
Strateji  : Order book duvarları + Trend dönüşü teyidi (canlı 5dk veri)
Teyit     : RSI diverjansı · EMA cross · Hacim spike · Mum formasyonu
TP / SL   : %2 / %1   R/R → 2:1
Borsa     : Binance Futures (API key gerekmez)

Kurulum   :
  pip install ccxt rich numpy pandas ta

Çalıştır  :
  python btc_signal_bot.py
"""

import time
import numpy as np
import pandas as pd
from datetime import datetime

# ── Paket kontrolü ──────────────────────────────────────────────────────────
try:
    import ccxt
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    import ta
except ImportError:
    import subprocess, sys
    print("Eksik paketler kuruluyor…")
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "ccxt", "rich", "numpy", "pandas", "ta", "-q"])
    import ccxt
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    import ta

# ════════════════════════════════════════════════════════════════════════════
#  AYARLAR
# ════════════════════════════════════════════════════════════════════════════
SYMBOL          = "BTC/USDT"
TIMEFRAME       = "5m"
CANDLE_LIMIT    = 100

# Order book
OB_DEPTH        = 200
TOP_WALLS       = 5
BUCKET_PCT      = 0.0015       # %0.15 fiyat gruplama
PROXIMITY_PCT   = 0.005        # Duvar yakınlık eşiği %0.5
MIN_WALL_BTC    = 4.0

# İndikatörler
EMA_FAST        = 9
EMA_SLOW        = 21
RSI_PERIOD      = 14
RSI_OB          = 65           # RSI aşırı alım eşiği
RSI_OS          = 35           # RSI aşırı satım eşiği
VOL_MULTIPLIER  = 1.8          # Hacim spike çarpanı

# Risk
TP_PCT          = 0.02
SL_PCT          = 0.01

# Minimum teyit skoru (0–4 arası, bu değerin altında sinyal yok)
MIN_SCORE       = 2

REFRESH_SEC     = 15

console  = Console()
exchange = ccxt.binance({"options": {"defaultType": "future"}})


# ════════════════════════════════════════════════════════════════════════════
#  VERİ ÇEKME
# ════════════════════════════════════════════════════════════════════════════
def fetch_ohlcv() -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=CANDLE_LIMIT)
    df  = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.astype({"open": float, "high": float, "low": float,
                      "close": float, "volume": float})


def fetch_orderbook() -> dict:
    return exchange.fetch_order_book(SYMBOL, OB_DEPTH)


def fetch_price() -> float:
    return float(exchange.fetch_ticker(SYMBOL)["last"])


# ════════════════════════════════════════════════════════════════════════════
#  İNDİKATÖR HESAPLAMA
# ════════════════════════════════════════════════════════════════════════════
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ema_fast"] = ta.trend.EMAIndicator(df["close"], EMA_FAST).ema_indicator()
    df["ema_slow"] = ta.trend.EMAIndicator(df["close"], EMA_SLOW).ema_indicator()
    df["rsi"]      = ta.momentum.RSIIndicator(df["close"], RSI_PERIOD).rsi()
    df["vol_ma"]   = df["volume"].rolling(20).mean()
    df["body"]     = df["close"] - df["open"]
    df["body_size"]= df["body"].abs()
    df["wick_up"]  = df["high"] - df[["open", "close"]].max(axis=1)
    df["wick_down"]= df[["open", "close"]].min(axis=1) - df["low"]
    return df


# ════════════════════════════════════════════════════════════════════════════
#  ORDER BOOK DUVARLARI
# ════════════════════════════════════════════════════════════════════════════
def cluster_walls(orders: list, ref: float, n: int) -> list:
    buckets: dict = {}
    for price, qty in orders:
        b = round(price / (ref * BUCKET_PCT)) * (ref * BUCKET_PCT)
        buckets[b] = buckets.get(b, 0) + qty
    buckets = {p: v for p, v in buckets.items() if v >= MIN_WALL_BTC}
    top = sorted(buckets.items(), key=lambda x: x[1], reverse=True)[:n]
    return sorted(top, key=lambda x: x[0])


# ════════════════════════════════════════════════════════════════════════════
#  MUM FORMASYONU TESPİTİ
# ════════════════════════════════════════════════════════════════════════════
def detect_candle_pattern(df: pd.DataFrame, is_long: bool):
    c = df.iloc[-1]
    p = df.iloc[-2]

    body_size = c["body_size"]
    wick_d    = c["wick_down"]
    wick_u    = c["wick_up"]
    body      = c["body"]

    if is_long:
        if wick_d > body_size * 2 and wick_u < body_size * 0.5:
            return "Hammer"
        if body > 0 and p["body"] < 0 and body_size > abs(p["body"]) * 1.2:
            return "Bullish Engulfing"
        if len(df) >= 3:
            pp = df.iloc[-3]
            if pp["body"] < 0 and abs(p["body"]) < abs(pp["body"]) * 0.4 and body > 0:
                return "Morning Star"
        if body > 0 and wick_d > body_size * 1.5:
            return "Bullish Pin Bar"
    else:
        if wick_u > body_size * 2 and wick_d < body_size * 0.5:
            return "Shooting Star"
        if body < 0 and p["body"] > 0 and body_size > p["body_size"] * 1.2:
            return "Bearish Engulfing"
        if len(df) >= 3:
            pp = df.iloc[-3]
            if pp["body"] > 0 and abs(p["body"]) < abs(pp["body"]) * 0.4 and body < 0:
                return "Evening Star"
        if body < 0 and wick_u > body_size * 1.5:
            return "Bearish Pin Bar"

    return None


# ════════════════════════════════════════════════════════════════════════════
#  TREND DÖNÜŞÜ PUANLAMA  (0–4 arası)
# ════════════════════════════════════════════════════════════════════════════
def score_reversal(df: pd.DataFrame, direction: str) -> tuple:
    score   = 0
    checks  = []
    c       = df.iloc[-1]
    p       = df.iloc[-2]
    is_long = direction == "LONG"

    # ── 1. EMA Dizilimi / Cross ──────────────────────────────────────────
    ema_cross_bull = (p["ema_fast"] < p["ema_slow"]) and (c["ema_fast"] > c["ema_slow"])
    ema_cross_bear = (p["ema_fast"] > p["ema_slow"]) and (c["ema_fast"] < c["ema_slow"])
    ema_bull_align = c["ema_fast"] > c["ema_slow"]

    if is_long and (ema_cross_bull or ema_bull_align):
        score += 1
        label  = f"EMA{EMA_FAST} > EMA{EMA_SLOW} kesişimi" if ema_cross_bull else f"EMA bullish dizilim"
        checks.append(("green", f"{label} ✓"))
    elif not is_long and (ema_cross_bear or not ema_bull_align):
        score += 1
        label  = f"EMA{EMA_FAST} < EMA{EMA_SLOW} kesişimi" if ema_cross_bear else f"EMA bearish dizilim"
        checks.append(("red", f"{label} ✓"))
    else:
        checks.append(("dim", "EMA: aleyhte"))

    # ── 2. RSI ──────────────────────────────────────────────────────────
    rsi     = c["rsi"]
    rsi_div = False

    if is_long and len(df) >= 5:
        low_p = df["low"].iloc[-5:]
        low_r = df["rsi"].iloc[-5:]
        if low_p.iloc[-1] <= low_p.min() and low_r.iloc[-1] > low_r.min():
            rsi_div = True
    elif not is_long and len(df) >= 5:
        hi_p = df["high"].iloc[-5:]
        hi_r = df["rsi"].iloc[-5:]
        if hi_p.iloc[-1] >= hi_p.max() and hi_r.iloc[-1] < hi_r.max():
            rsi_div = True

    if is_long and (rsi < RSI_OS or rsi_div):
        score += 1
        label  = f"RSI diverjans ({rsi:.0f})" if rsi_div else f"RSI aşırı satım ({rsi:.0f})"
        checks.append(("green", f"{label} ✓"))
    elif not is_long and (rsi > RSI_OB or rsi_div):
        score += 1
        label  = f"RSI diverjans ({rsi:.0f})" if rsi_div else f"RSI aşırı alım ({rsi:.0f})"
        checks.append(("red", f"{label} ✓"))
    else:
        checks.append(("dim", f"RSI: {rsi:.0f} (nötr)"))

    # ── 3. Hacim Spike ──────────────────────────────────────────────────
    vol_ratio = (c["volume"] / c["vol_ma"]) if c["vol_ma"] > 0 else 0
    if vol_ratio >= VOL_MULTIPLIER:
        score += 1
        checks.append(("yellow", f"Hacim spike x{vol_ratio:.1f} ✓"))
    else:
        checks.append(("dim", f"Hacim: x{vol_ratio:.1f} (normal)"))

    # ── 4. Mum Formasyonu ───────────────────────────────────────────────
    pattern = detect_candle_pattern(df, is_long)
    if pattern:
        score += 1
        clr = "green" if is_long else "red"
        checks.append((clr, f"{pattern} ✓"))
    else:
        checks.append(("dim", "Mum: net formasyon yok"))

    return score, checks


# ════════════════════════════════════════════════════════════════════════════
#  SİNYAL ÜRETİMİ
# ════════════════════════════════════════════════════════════════════════════
def generate_signals(price: float, bid_walls: list, ask_walls: list,
                     df: pd.DataFrame) -> list:
    signals = []

    for wp, wv in bid_walls:
        if wp >= price:
            continue
        dist = (price - wp) / price
        if dist > PROXIMITY_PCT:
            continue
        score, checks = score_reversal(df, "LONG")
        if score >= MIN_SCORE:
            signals.append({
                "dir": "LONG", "entry": price,
                "tp": round(price * (1 + TP_PCT), 2),
                "sl": round(price * (1 - SL_PCT), 2),
                "wall_price": wp, "wall_vol": wv,
                "dist_pct": dist * 100,
                "score": score, "checks": checks,
            })

    for wp, wv in ask_walls:
        if wp <= price:
            continue
        dist = (wp - price) / price
        if dist > PROXIMITY_PCT:
            continue
        score, checks = score_reversal(df, "SHORT")
        if score >= MIN_SCORE:
            signals.append({
                "dir": "SHORT", "entry": price,
                "tp": round(price * (1 - TP_PCT), 2),
                "sl": round(price * (1 + SL_PCT), 2),
                "wall_price": wp, "wall_vol": wv,
                "dist_pct": dist * 100,
                "score": score, "checks": checks,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


# ════════════════════════════════════════════════════════════════════════════
#  EKRAN RENDER
# ════════════════════════════════════════════════════════════════════════════
def stars(score: int, max_s: int = 4) -> str:
    colors = {1: "red", 2: "yellow", 3: "cyan", 4: "green"}
    clr    = colors.get(score, "white")
    return f"[{clr}]{'★' * score}[/{clr}][dim]{'☆' * (max_s - score)}[/dim]"


def render(price: float, prev_price, bid_walls: list, ask_walls: list,
           df: pd.DataFrame, signals: list):

    console.clear()
    ts    = datetime.now().strftime("%H:%M:%S")
    c     = df.iloc[-1]
    rsi   = c["rsi"]
    ema_f = c["ema_fast"]
    ema_s = c["ema_slow"]

    # Ok
    if prev_price is None:        arrow = ""
    elif price > prev_price:      arrow = " [green]▲[/green]"
    elif price < prev_price:      arrow = " [red]▼[/red]"
    else:                         arrow = " [dim]─[/dim]"

    ema_str = "[green]BULL ↑[/green]" if ema_f > ema_s else "[red]BEAR ↓[/red]"
    rsi_clr = "red" if rsi > RSI_OB else "green" if rsi < RSI_OS else "yellow"

    # ── Header ──────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold cyan]BTC/USDT · Sinyal Botu v2[/bold cyan]   [dim]{ts}  ·  {TIMEFRAME}[/dim]\n"
        f"Fiyat: [bold white]${price:,.2f}[/bold white]{arrow}     "
        f"RSI: [{rsi_clr}]{rsi:.1f}[/{rsi_clr}]     "
        f"EMA {EMA_FAST}/{EMA_SLOW}: {ema_str}",
        box=box.HEAVY, border_style="cyan"
    ))

    # ── Order Book Tablosu ───────────────────────────────────────────────
    wt = Table(title="📋  Order Book Duvarları", box=box.SIMPLE_HEAD,
               border_style="blue", header_style="bold blue")
    wt.add_column("Tür",         width=9,  justify="center")
    wt.add_column("Fiyat",       width=15, justify="right")
    wt.add_column("Hacim (BTC)", width=13, justify="right")
    wt.add_column("Uzaklık",     width=11, justify="right")
    wt.add_column("Ağırlık",     width=17)

    max_vol = max(([v for _, v in ask_walls] + [v for _, v in bid_walls]) or [1])

    for wp, wv in reversed(ask_walls):
        dist = (wp - price) / price * 100
        bar  = "█" * int(wv / max_vol * 14)
        near = " ⚡" if dist <= PROXIMITY_PCT * 100 else ""
        wt.add_row(f"[red]ASK 🔴[/red]",
                   f"[red]${wp:,.2f}[/red]",
                   f"[red]{wv:.1f}[/red]",
                   f"[red]+{dist:.2f}%{near}[/red]",
                   f"[red]{bar}[/red]")

    wt.add_row("──────", f"[bold yellow]${price:,.2f}[/bold yellow]",
               "[bold yellow]◀ SPOT[/bold yellow]", "", "")

    for wp, wv in reversed(bid_walls):
        dist = (price - wp) / price * 100
        bar  = "█" * int(wv / max_vol * 14)
        near = " ⚡" if dist <= PROXIMITY_PCT * 100 else ""
        wt.add_row(f"[green]BID 🟢[/green]",
                   f"[green]${wp:,.2f}[/green]",
                   f"[green]{wv:.1f}[/green]",
                   f"[green]-{dist:.2f}%{near}[/green]",
                   f"[green]{bar}[/green]")

    console.print(wt)
    console.print()

    # ── Sinyaller ───────────────────────────────────────────────────────
    if not signals:
        near_asks = [(wp, wv) for wp, wv in ask_walls
                     if (wp - price) / price <= PROXIMITY_PCT * 3]
        near_bids = [(wp, wv) for wp, wv in bid_walls
                     if (price - wp) / price <= PROXIMITY_PCT * 3]

        lines = []
        for wp, wv in near_asks:
            d = (wp - price) / price * 100
            lines.append(f"  [red]ASK ${wp:,.0f}  ({wv:.1f} BTC)  +{d:.2f}% uzakta[/red]")
        for wp, wv in near_bids:
            d = (price - wp) / price * 100
            lines.append(f"  [green]BID ${wp:,.0f}  ({wv:.1f} BTC)  -{d:.2f}% uzakta[/green]")

        body = "[dim]Aktif sinyal yok.[/dim]"
        if lines:
            body += "\n\n[dim]Yakın duvarlar:[/dim]\n" + "\n".join(lines)
        body += (f"\n\n[dim]Sinyal koşulları: fiyat duvar yakınında (<%{PROXIMITY_PCT*100:.1f}) "
                 f"VE ≥{MIN_SCORE}/4 teyit[/dim]")

        console.print(Panel(body, title="⏳  Bekleniyor", border_style="dim"))
    else:
        for s in signals:
            is_long  = s["dir"] == "LONG"
            clr      = "green" if is_long else "red"
            emoji    = "🟢 LONG" if is_long else "🔴 SHORT"
            strength = ["Zayıf", "Orta", "Güçlü", "Çok Güçlü"][min(s["score"] - 1, 3)]

            check_lines = "\n".join(
                f"    [{ch[0]}]{ch[1]}[/{ch[0]}]" for ch in s["checks"]
            )

            body = (
                f"  [bold]Yön      :[/bold] [{clr}]{emoji}[/{clr}]\n"
                f"  [bold]Güç      :[/bold] {stars(s['score'])}  [bold]{strength}[/bold]\n"
                f"  [bold]Giriş    :[/bold] [white]${s['entry']:,.2f}[/white]\n"
                f"  [bold]TP (+%2) :[/bold] [green]${s['tp']:,.2f}[/green]\n"
                f"  [bold]SL (-%1) :[/bold] [red]${s['sl']:,.2f}[/red]\n"
                f"  [bold]Duvar    :[/bold] ${s['wall_price']:,.2f}  "
                f"({s['wall_vol']:.1f} BTC)  "
                f"[dim]{s['dist_pct']:.3f}% uzakta[/dim]\n\n"
                f"  [bold]Teyitler :[/bold]\n{check_lines}"
            )
            console.print(Panel(body,
                                title=f"⚡  SİNYAL  [{s['score']}/4]",
                                border_style=clr))

    console.print(f"\n[dim]Yenileme: {REFRESH_SEC}sn  |  Çıkış: Ctrl+C[/dim]")


# ════════════════════════════════════════════════════════════════════════════
#  ANA DÖNGÜ
# ════════════════════════════════════════════════════════════════════════════
def main():
    console.print(Panel(
        "[bold cyan]BTC/USDT Sinyal Botu v2[/bold cyan]\n"
        "[dim]Canlı 5dk veri + Order book analizi başlatılıyor…[/dim]",
        box=box.DOUBLE, border_style="cyan"
    ))
    time.sleep(1)

    prev_price = None

    while True:
        try:
            price     = fetch_price()
            ob        = fetch_orderbook()
            df        = fetch_ohlcv()
            df        = calc_indicators(df)
            bid_walls = cluster_walls(ob["bids"], price, TOP_WALLS)
            ask_walls = cluster_walls(ob["asks"], price, TOP_WALLS)
            signals   = generate_signals(price, bid_walls, ask_walls, df)

            render(price, prev_price, bid_walls, ask_walls, df, signals)
            prev_price = price

        except KeyboardInterrupt:
            console.print("\n[yellow]Bot durduruldu. İyi trade'ler! 🚀[/yellow]")
            break
        except ccxt.NetworkError as e:
            console.print(f"[red]Ağ hatası: {e}  →  5sn sonra tekrar[/red]")
            time.sleep(5)
            continue
        except Exception as e:
            console.print(f"[red]Hata: {e}[/red]")
            import traceback; traceback.print_exc()
            time.sleep(5)
            continue

        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    main()
