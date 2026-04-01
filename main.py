from flask import Flask
import requests

app = Flask(__name__)

def get_price():
    url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    return float(requests.get(url).json()["price"])

@app.route("/")
def home():
    price = get_price()

    return f"""
    <h1>BTC Dashboard</h1>
    <h2>Price: {price}</h2>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
