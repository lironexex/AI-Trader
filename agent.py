"""
Multi-market Ollama trading agent for AI-Trader.
Handles: crypto + us-stock challenges in one loop.

Data sources (free, no API key):
  - CoinGecko: prices, OHLC history (RSI, volume ratio, trend)
  - Yahoo Finance: stock prices + history
  - alternative.me: Fear & Greed index
"""

import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

SERVER = os.getenv("TRADER_SERVER_URL", "http://localhost:8000")
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:14b")
TRADE_INTERVAL_SECONDS = int(os.getenv("TRADE_INTERVAL_SECONDS", "300"))
# Risk per trade = % of current portfolio
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", "0.03"))  # 3%
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "5000"))

AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}
COINGECKO = "https://api.coingecko.com/api/v3"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

WATCH_STOCKS = ["AAPL", "TSLA", "NVDA", "MSFT", "META", "AMZN", "GOOGL", "SPY", "QQQ"]
CRYPTO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "DOGE": "dogecoin",
    "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink",
}


# ── technical indicators ──────────────────────────────────────────────────────

def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def compute_sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 4)


def volume_ratio(volumes: list[float], current_vol: float, period: int = 20) -> float | None:
    if len(volumes) < period:
        return None
    avg = sum(volumes[-period:]) / period
    return round(current_vol / avg, 2) if avg > 0 else None


# ── market data ──────────────────────────────────────────────────────────────

def fetch_crypto_with_indicators() -> dict:
    ids_param = ",".join(CRYPTO_IDS.values())
    result = {}

    # Current prices
    try:
        r = requests.get(
            f"{COINGECKO}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids_param,
                "order": "market_cap_desc",
                "per_page": 20,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=15,
        )
        for c in r.json():
            sym = c["symbol"].upper()
            result[sym] = {
                "price": c["current_price"],
                "change_1h_pct": c.get("price_change_percentage_1h_in_currency"),
                "change_24h_pct": c.get("price_change_percentage_24h"),
                "change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
                "volume_24h": c.get("total_volume"),
                "market_cap": c.get("market_cap"),
            }
    except Exception as e:
        print(f"[DATA] CoinGecko prices: {e}")
        return {}

    # OHLC for RSI + SMA on key coins
    for sym, coin_id in [("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana")]:
        try:
            r = requests.get(
                f"{COINGECKO}/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": "30"},
                timeout=15,
            )
            ohlc = r.json()  # [[ts, o, h, l, c], ...]
            if not ohlc or not isinstance(ohlc, list):
                continue
            closes = [candle[4] for candle in ohlc]
            vols = result.get(sym, {}).get("volume_24h")
            rsi = compute_rsi(closes)
            sma20 = compute_sma(closes, 20)
            sma50 = compute_sma(closes, 50)
            price = result.get(sym, {}).get("price")
            if sym in result:
                result[sym]["rsi_14"] = rsi
                result[sym]["sma_20"] = sma20
                result[sym]["sma_50"] = sma50
                result[sym]["above_sma20"] = (price > sma20) if (price and sma20) else None
                result[sym]["above_sma50"] = (price > sma50) if (price and sma50) else None
            time.sleep(0.5)  # CoinGecko rate limit
        except Exception as e:
            print(f"[DATA] OHLC {sym}: {e}")

    return result


def fetch_stock_with_indicators() -> dict:
    result = {}
    # Current quotes
    try:
        syms = ",".join(WATCH_STOCKS)
        r = requests.get(
            f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}",
            headers=YAHOO_HEADERS,
            timeout=15,
        )
        for q in r.json().get("quoteResponse", {}).get("result", []):
            sym = q["symbol"]
            result[sym] = {
                "price": q.get("regularMarketPrice"),
                "change_24h_pct": q.get("regularMarketChangePercent"),
                "volume": q.get("regularMarketVolume"),
                "avg_volume_10d": q.get("averageDailyVolume10Day"),
                "market_state": q.get("marketState"),
                "52w_high": q.get("fiftyTwoWeekHigh"),
                "52w_low": q.get("fiftyTwoWeekLow"),
            }
    except Exception as e:
        print(f"[DATA] Yahoo quotes: {e}")
        return {}

    # RSI + SMA for key stocks (fetch 30-day history)
    for sym in ["NVDA", "AAPL", "TSLA", "SPY"]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval": "1d", "range": "60d"},
                headers=YAHOO_HEADERS,
                timeout=15,
            )
            data = r.json()["chart"]["result"][0]
            closes = data["indicators"]["quote"][0]["close"]
            volumes = data["indicators"]["quote"][0].get("volume", [])
            closes = [c for c in closes if c is not None]
            volumes = [v for v in volumes if v is not None]
            price = result.get(sym, {}).get("price")
            avg_vol = volume_ratio(volumes[:-1], volumes[-1] if volumes else 0)
            rsi = compute_rsi(closes)
            sma20 = compute_sma(closes, 20)
            sma50 = compute_sma(closes, 50)
            if sym in result:
                result[sym]["rsi_14"] = rsi
                result[sym]["sma_20"] = sma20
                result[sym]["sma_50"] = sma50
                result[sym]["volume_vs_avg"] = avg_vol
                result[sym]["above_sma20"] = (price > sma20) if (price and sma20) else None
                result[sym]["above_sma50"] = (price > sma50) if (price and sma50) else None
        except Exception as e:
            print(f"[DATA] Yahoo history {sym}: {e}")

    return result


def fetch_fear_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=3", timeout=10)
        data = r.json()["data"]
        return {
            "today": {"value": int(data[0]["value"]), "label": data[0]["value_classification"]},
            "yesterday": {"value": int(data[1]["value"]), "label": data[1]["value_classification"]},
            "trend": "improving" if int(data[0]["value"]) > int(data[1]["value"]) else "worsening",
        }
    except Exception as e:
        print(f"[DATA] Fear&Greed: {e}")
        return {}


def fetch_trending_crypto() -> list:
    try:
        r = requests.get(f"{COINGECKO}/search/trending", timeout=10)
        return [
            {"symbol": c["item"]["symbol"].upper(), "name": c["item"]["name"]}
            for c in r.json().get("coins", [])[:5]
        ]
    except Exception:
        return []


# ── agent state ──────────────────────────────────────────────────────────────

def fetch_positions() -> list:
    try:
        r = requests.get(f"{SERVER}/api/positions", headers=AUTH, timeout=10)
        return r.json().get("positions", [])
    except Exception:
        return []


def fetch_cash() -> float:
    try:
        r = requests.get(f"{SERVER}/api/claw/agents/me", headers=AUTH, timeout=10)
        return float(r.json().get("cash", 100000))
    except Exception:
        return 100000.0


# ── LLM decision ─────────────────────────────────────────────────────────────

def ask_llm(market_data: dict, positions: list, cash: float) -> list:
    portfolio_value = cash  # simplified; add position values for accuracy
    risk_usd = min(portfolio_value * RISK_PCT_PER_TRADE, MAX_POSITION_USD)

    open_pos = [
        {
            "symbol": p["symbol"], "market": p["market"],
            "side": p["side"], "qty": p["quantity"],
            "entry_price": p["entry_price"],
            "unrealized_pnl_pct": round(
                ((market_data.get("crypto", {}).get(p["symbol"], {}).get("price") or p["entry_price"]) / p["entry_price"] - 1) * 100, 2
            ) if p["market"] == "crypto" else None,
        }
        for p in positions[:8]
    ]

    stock_open = any(
        s.get("market_state") == "REGULAR"
        for s in market_data.get("stocks", {}).values()
    )

    prompt = f"""You are a disciplined, data-driven paper trading agent. Real money rules apply.

Portfolio cash: ${cash:,.0f}
Risk budget per trade: ${risk_usd:,.0f} (3% of portfolio)
US market: {"OPEN" if stock_open else "CLOSED — skip stock trades"}

=== FEAR & GREED ===
{json.dumps(market_data.get("fear_greed", {}), indent=2)}

=== CRYPTO (with RSI, SMA, % changes) ===
{json.dumps(market_data.get("crypto", {}), indent=2)}

=== TRENDING COINS ===
{json.dumps(market_data.get("trending", []), indent=2)}

=== US STOCKS (with RSI, SMA, volume vs avg) ===
{json.dumps(market_data.get("stocks", {}), indent=2)}

=== OPEN POSITIONS ===
{json.dumps(open_pos, indent=2) if open_pos else "none"}

Trading rules you must follow:
- RSI < 30 = oversold (buy signal). RSI > 70 = overbought (sell/short signal).
- Only buy when price is above SMA-20 (uptrend). Only short when below SMA-20.
- Volume spike (volume_vs_avg > 1.5) confirms breakout — stronger signal.
- Close losing positions if unrealized_pnl_pct < -5% (stop loss).
- Close winning positions if unrealized_pnl_pct > 10% (take profit).
- Max 1 crypto trade + 1 stock trade per cycle.
- quantity * price must NOT exceed ${risk_usd:,.0f}.
- Only sell/cover if you hold that position.
- Never trade stocks when US market is CLOSED.
- Fees are 0.15% per trade — factor this into your edge calculation.

Return ONLY a JSON array, no markdown:
[
  {{
    "action": "buy" | "sell" | "short" | "cover" | "hold",
    "symbol": "BTC",
    "market": "crypto",
    "quantity": 0.05,
    "reason": "RSI=28 oversold, above SMA20, volume spike confirms"
  }},
  {{
    "action": "hold",
    "symbol": "",
    "market": "us-stock",
    "quantity": 0,
    "reason": "market closed"
  }}
]
"""

    try:
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 512,
            },
            timeout=120,
        )
        content = r.json()["choices"][0]["message"]["content"].strip()
        if "```" in content:
            parts = content.split("```")
            content = parts[1] if len(parts) > 1 else parts[0]
            if content.startswith("json"):
                content = content[4:]
            content = content.split("```")[0]
        result = json.loads(content.strip())
        return result if isinstance(result, list) else [result]
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return []


# ── trade execution ───────────────────────────────────────────────────────────

def get_price(symbol: str, market: str, market_data: dict) -> float | None:
    if market == "crypto":
        return market_data.get("crypto", {}).get(symbol.upper(), {}).get("price")
    if market == "us-stock":
        raw = market_data.get("stocks", {}).get(symbol.upper(), {}).get("price")
        return raw
    return None


def execute_trade(decision: dict, price: float, action: str) -> bool:
    # Simulate bid/ask spread for stocks (server doesn't auto-fetch stock price)
    market = decision.get("market", "crypto")
    if market == "us-stock":
        spread = 0.0005  # 0.05% each side
        exec_price = price * (1 + spread) if action in ("buy", "short") else price * (1 - spread)
    else:
        exec_price = price  # server fetches its own Hyperliquid price for crypto

    payload = {
        "market": market,
        "action": action,
        "symbol": decision["symbol"],
        "price": exec_price,
        "quantity": float(decision["quantity"]),
        "content": decision.get("reason", ""),
        "executed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        r = requests.post(
            f"{SERVER}/api/signals/realtime",
            headers={**AUTH, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            cost = exec_price * float(decision["quantity"])
            print(f"  [OK] {action.upper()} {decision['quantity']} {decision['symbol']} @ ${exec_price:,.4f} (${cost:,.0f}) | {decision.get('reason','')}")
            return True
        else:
            print(f"  [FAIL] {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  [ERR] {e}")
        return False


# ── main loop ─────────────────────────────────────────────────────────────────

def run():
    if not AGENT_TOKEN or AGENT_TOKEN == "YOUR_TOKEN_HERE":
        print("ERROR: Set AGENT_TOKEN in .env")
        return

    print(f"Agent online | model={LLM_MODEL} | interval={TRADE_INTERVAL_SECONDS}s | risk={RISK_PCT_PER_TRADE*100:.0f}%/trade")

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] Fetching market data + indicators...")

        crypto = fetch_crypto_with_indicators()
        stocks = fetch_stock_with_indicators()
        fear_greed = fetch_fear_greed()
        trending = fetch_trending_crypto()
        positions = fetch_positions()
        cash = fetch_cash()

        btc = crypto.get("BTC", {})
        fg = fear_greed.get("today", {})
        if btc:
            rsi_str = f" RSI={btc['rsi_14']}" if btc.get("rsi_14") else ""
            print(f"  BTC=${btc['price']:,.0f} ({btc.get('change_24h_pct', 0):+.1f}%){rsi_str} | F&G={fg.get('value','?')} {fg.get('label','')}")

        market_data = {"crypto": crypto, "stocks": stocks, "fear_greed": fear_greed, "trending": trending}

        print(f"  Asking {LLM_MODEL}...")
        decisions = ask_llm(market_data, positions, cash)

        for d in decisions:
            action = (d.get("action") or "hold").lower()
            if action == "hold" or not d.get("symbol"):
                print(f"  [HOLD] {d.get('reason','')}")
                continue
            symbol = d["symbol"].upper()
            market = d.get("market", "crypto")
            price = get_price(symbol, market, market_data)
            if not price:
                print(f"  [SKIP] No price for {symbol}")
                continue
            execute_trade(d, price, action)

        print(f"[SLEEP] Next in {TRADE_INTERVAL_SECONDS}s")
        time.sleep(TRADE_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
