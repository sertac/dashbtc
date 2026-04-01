from flask import Flask, jsonify, render_template_string
import time
import random
import math

app = Flask(__name__)

# =========================
# STATE
# =========================
state = {
    "price": 100.0,
    "signal": None,
    "confidence": 0,

    "active_trade": None,

    "portfolio": {
        "balance": 10000,
        "equity": 10000,
        "peak": 10000,
        "drawdown": 0,
        "trades": []
    },

    "equity_curve": []
}

symbols = ["BTC", "ETH"]  # multi symbol sim

# =========================
# PRICE FEED (SIM MULTI-ASSET)
# =========================
def fetch_price():
    base = state["price"]
    drift = random.uniform(-2, 2)
    state["price"] = max(1, base + drift)
    return state["price"]

# =========================
# SIGNAL ENGINE (IMPROVED + FILTER)
# =========================
def signal_engine(price):
    r = random.random()

    # fake "trend bias"
    trend = math.sin(time.time() / 10)

    confidence = abs(trend + random.uniform(-0.3, 0.3))

    if confidence < 0.6:
        return None, 0

    if r > 0.7 and trend > 0:
        return "LONG", confidence
    elif r < 0.3 and trend < 0:
        return "SHORT", confidence

    return None, confidence


# =========================
# POSITION SIZE
# =========================
def position_size(entry, sl):
    risk = state["portfolio"]["balance"] * 0.01
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0:
        return 0
    return risk / risk_per_unit


# =========================
# OPEN TRADE
# =========================
def open_trade(side, price, confidence):
    if confidence < 0.65:
        return  # filter weak signals

    if side == "LONG":
        sl = price - 2
        tp = price + 3
    else:
        sl = price + 2
        tp = price - 3

    qty = position_size(price, sl)

    state["active_trade"] = {
        "side": side,
        "entry": price,
        "tp": tp,
        "sl": sl,
        "qty": qty,
        "time": time.time()
    }


# =========================
# TRADE ENGINE
# =========================
def update_trade(price):
    trade = state["active_trade"]
    if not trade:
        return

    port = state["portfolio"]
    pnl = 0

    if trade["side"] == "LONG":
        if price >= trade["tp"]:
            pnl = trade["qty"] * (trade["tp"] - trade["entry"])
        elif price <= trade["sl"]:
            pnl = trade["qty"] * (trade["sl"] - trade["entry"])

    if trade["side"] == "SHORT":
        if price <= trade["tp"]:
            pnl = trade["qty"] * (trade["entry"] - trade["tp"])
        elif price >= trade["sl"]:
            pnl = trade["qty"] * (trade["entry"] - trade["sl"])

    if pnl != 0:
        port["balance"] += pnl
        port["equity"] = port["balance"]

        port["trades"].append(pnl)

        state["active_trade"] = None

    # drawdown
    if port["equity"] > port["peak"]:
        port["peak"] = port["equity"]

    port["drawdown"] = (port["peak"] - port["equity"]) / port["peak"]

    # equity curve
    state["equity_curve"].append(port["equity"])
    if len(state["equity_curve"]) > 200:
        state["equity_curve"].pop(0)


# =========================
# METRICS
# =========================
def winrate():
    t = state["portfolio"]["trades"]
    if not t:
        return 0
    return len([x for x in t if x > 0]) / len(t)


def sharpe():
    t = state["portfolio"]["trades"]
    if len(t) < 2:
        return 0

    avg = sum(t) / len(t)
    std = (sum((x - avg) ** 2 for x in t) / len(t)) ** 0.5
    if std == 0:
        return 0
    return avg / std


def health():
    return round(winrate()*50 + (1-state["portfolio"]["drawdown"])*30 + min(20, sharpe()*10), 2)


# =========================
# STEP ENGINE
# =========================
def step():
    price = fetch_price()

    sig, conf = signal_engine(price)

    state["signal"] = sig
    state["confidence"] = conf

    update_trade(price)

    if sig and state["active_trade"] is None:
        open_trade(sig, price, conf)


# =========================
# DASHBOARD
# =========================
HTML = """
<!DOCTYPE html>
<html>
<head>
<style>
body { font-family: Arial; background:#111; color:#eee; }
.box { padding:10px; margin:10px; background:#222; border-radius:10px; }
.long { background: rgba(0,255,0,0.12); }
.short { background: rgba(255,0,0,0.12); }
canvas { background:#000; margin:10px; }
</style>
</head>
<body>

<h2>V8 QUANT TERMINAL</h2>

<div class="box">
Price: <span id="price"></span><br>
Signal: <span id="signal"></span><br>
Confidence: <span id="conf"></span>
</div>

<div class="box">
Balance: <span id="bal"></span><br>
Drawdown: <span id="dd"></span><br>
Winrate: <span id="wr"></span><br>
Sharpe: <span id="sh"></span><br>
Health: <span id="hs"></span>
</div>

<canvas id="chart" width="600" height="200"></canvas>

<script>
let ctx = document.getElementById("chart").getContext("2d");

function drawChart(data){
    ctx.clearRect(0,0,600,200);

    ctx.beginPath();
    for(let i=0;i<data.length;i++){
        let x = i * (600/data.length);
        let y = 200 - (data[i] / 100);
        ctx.lineTo(x,y);
    }
    ctx.strokeStyle = "lime";
    ctx.stroke();
}

async function update(){
    let r = await fetch('/api');
    let d = await r.json();

    document.body.className = d.signal === "LONG" ? "long" :
                              d.signal === "SHORT" ? "short" : "";

    document.getElementById("price").innerText = d.price.toFixed(2);
    document.getElementById("signal").innerText = d.signal;
    document.getElementById("conf").innerText = d.confidence.toFixed(2);

    document.getElementById("bal").innerText = d.portfolio.balance.toFixed(2);
    document.getElementById("dd").innerText = (d.portfolio.drawdown*100).toFixed(2)+"%";
    document.getElementById("wr").innerText = (d.winrate*100).toFixed(2)+"%";
    document.getElementById("sh").innerText = d.sharpe.toFixed(2);
    document.getElementById("hs").innerText = d.health;

    drawChart(d.equity_curve);
}

setInterval(update,1000);
update();
</script>

</body>
</html>
"""


# =========================
# API
# =========================
@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api")
def api():
    step()
    return jsonify({
        "price": state["price"],
        "signal": state["signal"],
        "confidence": state["confidence"],
        "portfolio": state["portfolio"],
        "winrate": winrate(),
        "sharpe": sharpe(),
        "health": health(),
        "equity_curve": state["equity_curve"]
    })


if __name__ == "__main__":
    app.run(debug=True)
