"""
Multi-market Ollama trading agent for AI-Trader.
Handles: crypto, us-stock, polymarket — one agent, all challenges.

Data sources (all free, no API key):
  - CoinGecko: crypto prices + trending + news
  - Yahoo Finance: US stock prices + % change
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
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "5000"))

AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}
COINGECKO = "https://api.coingecko.com/api/v3"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

WATCH_STOCKS = ["AAPL", "TSLA", "NVDA", "MSFT", "META", "AMZN", "GOOGL", "SPY", "QQQ"]
WATCH_CRYPTO_IDS = "bitcoin,ethereum,solana,binancecoin,ripple,dogecoin,cardano,avalanche-2,chainlink,polkadot"


# ── market data ──────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict:
    try:
        r = requests.get(
            f"{COINGECKO}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": WATCH_CRYPTO_IDS,
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=15,
        )
        return {
            c["symbol"].upper(): {
                "price": c["current_price"],
                "change_1h_pct": c.get("price_change_percentage_1h_in_currency"),
                "change_24h_pct": c.get("price_change_percentage_24h"),
                "change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
                "volume_24h_usd": c.get("total_volume"),
            }
            for c in r.json()
        }
    except Exception as e:
        print(f"[DATA] CoinGecko error: {e}")
        return {}


def fetch_stock_prices() -> dict:
    try:
        symbols = ",".join(WATCH_STOCKS)
        r = requests.get(
            f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}",
            headers=YAHOO_HEADERS,
            timeout=15,
        )
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        return {
            q["symbol"]: {
                "price": q.get("regularMarketPrice"),
                "change_24h_pct": q.get("regularMarketChangePercent"),
                "volume": q.get("regularMarketVolume"),
                "market_state": q.get("marketState"),  # REGULAR, PRE, POST, CLOSED
            }
            for q in quotes
            if q.get("regularMarketPrice")
        }
    except Exception as e:
        print(f"[DATA] Yahoo Finance error: {e}")
        return {}


def fetch_fear_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        data = r.json()["data"]
        return {
            "value": int(data[0]["value"]),
            "label": data[0]["value_classification"],
            "yesterday": int(data[1]["value"]),
        }
    except Exception as e:
        print(f"[DATA] Fear&Greed error: {e}")
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


def fetch_crypto_news() -> list:
    try:
        r = requests.get(f"{COINGECKO}/news", timeout=10)
        return [
            {"title": n.get("title", ""), "snippet": (n.get("description") or "")[:100]}
            for n in r.json().get("data", [])[:5]
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
    """Returns list of trade decisions (one per market)."""

    open_pos = [
        {"symbol": p["symbol"], "market": p["market"], "side": p["side"],
         "qty": p["quantity"], "entry": p["entry_price"]}
        for p in positions[:8]
    ]

    stock_state = ""
    for sym, d in (market_data.get("stocks") or {}).items():
        if d.get("market_state") == "REGULAR":
            stock_state = "US market is OPEN"
            break
    else:
        stock_state = "US market may be CLOSED (check market_state field)"

    prompt = f"""You are an aggressive multi-market paper trading agent.
Cash: ${cash:,.0f} | Max risk per trade: ${MAX_POSITION_USD:,.0f}
{stock_state}

=== CRYPTO MARKET ===
Fear & Greed: {json.dumps(market_data.get('fear_greed', {}), indent=2)}
Trending: {json.dumps(market_data.get('trending', []), indent=2)}
Prices (with % change):
{json.dumps(market_data.get('crypto', {}), indent=2)}

=== US STOCKS ===
{json.dumps(market_data.get('stocks', {}), indent=2)}

=== NEWS ===
{json.dumps(market_data.get('news', []), indent=2)}

=== MY OPEN POSITIONS ===
{json.dumps(open_pos, indent=2) if open_pos else 'none'}

Decide up to 2 trades (one crypto, one stock) OR hold for each.
Return a JSON array of decisions. Each item:
{{
  "action": "buy" | "sell" | "short" | "cover" | "hold",
  "symbol": "BTC",
  "market": "crypto" | "us-stock",
  "quantity": 0.05,
  "reason": "one sentence"
}}

Rules:
- Return ONLY the JSON array, no markdown.
- For crypto: symbols like BTC, ETH, SOL. market="crypto".
- For stocks: symbols like AAPL, TSLA. market="us-stock". Only trade when market_state=REGULAR.
- quantity * price must not exceed ${MAX_POSITION_USD:,.0f}.
- Only sell/cover if you have an open position in that symbol.
- Fear < 30 = buy crypto. Fear > 75 = consider short/sell.
- Prefer action over hold — generate returns.
- If nothing to do, return [{{"action":"hold","symbol":"","market":"crypto","quantity":0,"reason":"no signal"}}]
"""

    try:
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
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
        result = json.loads(content.strip())
        return result if isinstance(result, list) else [result]
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return []


# ── trade execution ───────────────────────────────────────────────────────────

def get_price_from_data(symbol: str, market: str, market_data: dict) -> float | None:
    if market == "crypto":
        return market_data.get("crypto", {}).get(symbol.upper(), {}).get("price")
    if market == "us-stock":
        return market_data.get("stocks", {}).get(symbol.upper(), {}).get("price")
    return None


def execute_trade(decision: dict, price: float) -> bool:
    payload = {
        "market": decision["market"],
        "action": decision["action"],
        "symbol": decision["symbol"],
        "price": price,
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
            cost = price * float(decision["quantity"])
            print(f"  [TRADE OK] {decision['action'].upper()} {decision['quantity']} {decision['symbol']} @ ${price:,.4f} (${cost:,.0f}) | {decision.get('reason','')}")
            return True
        else:
            print(f"  [TRADE FAIL] {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  [TRADE ERR] {e}")
        return False


# ── main loop ─────────────────────────────────────────────────────────────────

def run():
    if not AGENT_TOKEN or AGENT_TOKEN == "YOUR_TOKEN_HERE":
        print("ERROR: Set AGENT_TOKEN in .env")
        return

    print(f"Agent online | model={LLM_MODEL} | markets=crypto+us-stock | interval={TRADE_INTERVAL_SECONDS}s")

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] Fetching market data...")

        crypto = fetch_crypto_prices()
        stocks = fetch_stock_prices()
        fear_greed = fetch_fear_greed()
        trending = fetch_trending_crypto()
        news = fetch_crypto_news()
        positions = fetch_positions()
        cash = fetch_cash()

        # quick summary
        btc = crypto.get("BTC", {})
        if btc:
            print(f"  BTC=${btc['price']:,.0f} ({btc['change_24h_pct']:+.1f}%) | F&G={fear_greed.get('value','?')} {fear_greed.get('label','')}")
        stock_open = any(s.get("market_state") == "REGULAR" for s in stocks.values())
        nvda = stocks.get("NVDA", {})
        if nvda:
            state = "OPEN" if stock_open else "CLOSED"
            print(f"  NVDA=${nvda.get('price',0):,.2f} ({nvda.get('change_24h_pct',0):+.2f}%) | US market {state}")

        market_data = {"crypto": crypto, "stocks": stocks, "fear_greed": fear_greed, "trending": trending, "news": news}

        print(f"  Asking {LLM_MODEL} for decisions...")
        decisions = ask_llm(market_data, positions, cash)

        for d in decisions:
            if not d or d.get("action") == "hold":
                print(f"  [HOLD] {d.get('reason','')}")
                continue
            symbol = d.get("symbol", "").upper()
            market = d.get("market", "crypto")
            price = get_price_from_data(symbol, market, market_data)
            if not price:
                print(f"  [SKIP] No price for {symbol} ({market})")
                continue
            execute_trade(d, price)

        print(f"[SLEEP] Next in {TRADE_INTERVAL_SECONDS}s")
        time.sleep(TRADE_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
