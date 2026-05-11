"""
Local Ollama trading agent for AI-Trader.
Fetches real market data from free public APIs (no key needed).

Loop:
  1. Fetch live crypto/stock data + fear&greed + news
  2. Ask Ollama to decide: trade or hold
  3. If trade → POST /api/signals/realtime
  4. Sleep TRADE_INTERVAL_SECONDS and repeat
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


# ── market data ──────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict:
    """Top 10 coins with 1h/24h/7d price change."""
    try:
        r = requests.get(
            f"{COINGECKO}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=15,
        )
        coins = r.json()
        return {
            c["symbol"].upper(): {
                "price": c["current_price"],
                "change_1h": c.get("price_change_percentage_1h_in_currency"),
                "change_24h": c.get("price_change_percentage_24h"),
                "change_7d": c.get("price_change_percentage_7d_in_currency"),
                "volume_24h": c.get("total_volume"),
                "market_cap": c.get("market_cap"),
            }
            for c in coins
        }
    except Exception as e:
        print(f"[DATA] CoinGecko prices error: {e}")
        return {}


def fetch_fear_greed() -> dict:
    """Crypto Fear & Greed Index (0=extreme fear, 100=extreme greed)."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        data = r.json()["data"]
        return {
            "value": int(data[0]["value"]),
            "classification": data[0]["value_classification"],
            "yesterday": int(data[1]["value"]),
        }
    except Exception as e:
        print(f"[DATA] Fear&Greed error: {e}")
        return {}


def fetch_trending_coins() -> list:
    """CoinGecko trending coins (what's hot right now)."""
    try:
        r = requests.get(f"{COINGECKO}/search/trending", timeout=10)
        coins = r.json().get("coins", [])
        return [
            {
                "symbol": c["item"]["symbol"].upper(),
                "name": c["item"]["name"],
                "rank": c["item"]["market_cap_rank"],
            }
            for c in coins[:7]
        ]
    except Exception as e:
        print(f"[DATA] Trending error: {e}")
        return []


def fetch_crypto_news() -> list:
    """Latest crypto news from CoinGecko."""
    try:
        r = requests.get(f"{COINGECKO}/news", timeout=10)
        items = r.json().get("data", [])[:6]
        return [
            {"title": n.get("title", ""), "description": (n.get("description") or "")[:120]}
            for n in items
        ]
    except Exception as e:
        print(f"[DATA] News error: {e}")
        return []


def fetch_btc_dominance() -> float | None:
    """BTC dominance % from global market data."""
    try:
        r = requests.get(f"{COINGECKO}/global", timeout=10)
        return round(r.json()["data"]["market_cap_percentage"].get("btc", 0), 1)
    except Exception:
        return None


# ── agent state ──────────────────────────────────────────────────────────────

def fetch_my_positions() -> list:
    try:
        r = requests.get(f"{SERVER}/api/positions", headers=AUTH, timeout=10)
        return r.json().get("positions", [])
    except Exception:
        return []


def fetch_my_cash() -> float:
    try:
        r = requests.get(f"{SERVER}/api/claw/agents/me", headers=AUTH, timeout=10)
        return float(r.json().get("cash", 100000))
    except Exception:
        return 100000.0


# ── LLM decision ─────────────────────────────────────────────────────────────

def ask_llm(market_data: dict, positions: list, cash: float) -> dict | None:
    open_pos_summary = json.dumps(
        [{"symbol": p["symbol"], "side": p["side"], "qty": p["quantity"], "entry": p["entry_price"]} for p in positions[:5]],
        indent=2,
    ) if positions else "none"

    prompt = f"""You are an aggressive autonomous crypto paper trading agent.
Available cash: ${cash:,.0f}
Max risk per trade: ${MAX_POSITION_USD:,.0f}

=== FEAR & GREED INDEX ===
{json.dumps(market_data.get('fear_greed', {}), indent=2)}

=== TOP CRYPTO PRICES (with % change) ===
{json.dumps(market_data.get('prices', {}), indent=2)}

=== BTC DOMINANCE ===
{market_data.get('btc_dominance', 'unknown')}%

=== TRENDING COINS ===
{json.dumps(market_data.get('trending', []), indent=2)}

=== RECENT NEWS ===
{json.dumps(market_data.get('news', []), indent=2)}

=== MY OPEN POSITIONS ===
{open_pos_summary}

Analyze the data and decide ONE trade to make right now, or hold.
Look for: momentum, fear/greed extremes, trending coins, news catalysts.

Respond with ONLY valid JSON (no markdown, no explanation outside JSON):
{{
  "action": "buy" | "sell" | "short" | "cover" | "hold",
  "symbol": "BTC",
  "market": "crypto",
  "quantity": 0.05,
  "reason": "one sentence"
}}

Rules:
- market must be: crypto
- quantity * price must not exceed ${MAX_POSITION_USD:,.0f}
- Only sell/cover if you have that open position
- Fear < 30 = buy opportunity. Fear > 75 = consider selling/shorting.
- When unsure, small buy is better than hold — you need to generate returns.
"""

    try:
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 300,
            },
            timeout=120,
        )
        content = r.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.split("```")[0]
        return json.loads(content.strip())
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return None


# ── trade execution ───────────────────────────────────────────────────────────

def get_live_price(symbol: str, prices: dict) -> float | None:
    return prices.get(symbol.upper(), {}).get("price")


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
            print(f"[TRADE] ✓ {decision['action'].upper()} {decision['quantity']} {decision['symbol']} @ ${price:,.2f} (${cost:,.0f})")
            return True
        else:
            print(f"[TRADE] ✗ {r.status_code}: {r.text[:300]}")
            return False
    except Exception as e:
        print(f"[TRADE] Error: {e}")
        return False


# ── main loop ─────────────────────────────────────────────────────────────────

def run():
    if not AGENT_TOKEN or AGENT_TOKEN == "YOUR_TOKEN_HERE":
        print("ERROR: Set AGENT_TOKEN in .env")
        return

    print(f"Agent online | model={LLM_MODEL} | server={SERVER} | interval={TRADE_INTERVAL_SECONDS}s")

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] Fetching live market data...")

        prices = fetch_crypto_prices()
        fear_greed = fetch_fear_greed()
        trending = fetch_trending_coins()
        news = fetch_crypto_news()
        btc_dom = fetch_btc_dominance()
        positions = fetch_my_positions()
        cash = fetch_my_cash()

        if prices:
            btc = prices.get("BTC", {})
            fg = fear_greed.get("value", "?")
            print(f"  BTC=${btc.get('price', '?'):,.0f}  24h={btc.get('change_24h', '?'):+.1f}%  F&G={fg} ({fear_greed.get('classification', '?')})")

        market_data = {
            "prices": prices,
            "fear_greed": fear_greed,
            "trending": trending,
            "news": news,
            "btc_dominance": btc_dom,
        }

        print(f"  Asking {LLM_MODEL}...")
        decision = ask_llm(market_data, positions, cash)

        if not decision:
            print("[SKIP] No valid decision from LLM")
        elif decision.get("action") == "hold":
            print(f"[HOLD] {decision.get('reason', '')}")
        else:
            symbol = decision.get("symbol", "BTC").upper()
            price = get_live_price(symbol, prices)
            if not price:
                print(f"[SKIP] No price data for {symbol}")
            else:
                print(f"  Decision: {decision['action'].upper()} {decision['quantity']} {symbol} — {decision.get('reason', '')}")
                execute_trade(decision, price)

        print(f"[SLEEP] Next run in {TRADE_INTERVAL_SECONDS}s")
        time.sleep(TRADE_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
