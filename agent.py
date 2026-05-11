"""
Local Ollama trading agent for AI-Trader.

Loop:
  1. Fetch market intel from local server
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


def fetch_market_intel() -> dict:
    out = {}
    try:
        out["news"] = requests.get(f"{SERVER}/api/market-intel/news?limit=5", timeout=10).json()
    except Exception:
        out["news"] = {}
    try:
        out["macro"] = requests.get(f"{SERVER}/api/market-intel/macro-signals", timeout=10).json()
    except Exception:
        out["macro"] = {}
    try:
        out["etf"] = requests.get(f"{SERVER}/api/market-intel/etf-flows", timeout=10).json()
    except Exception:
        out["etf"] = {}
    try:
        out["stocks"] = requests.get(f"{SERVER}/api/market-intel/stocks/featured?limit=6", timeout=10).json()
    except Exception:
        out["stocks"] = {}
    return out


def fetch_my_positions() -> list:
    try:
        r = requests.get(f"{SERVER}/api/positions", headers=AUTH, timeout=10)
        return r.json().get("positions", [])
    except Exception:
        return []


def fetch_price(symbol: str, market: str) -> float | None:
    try:
        r = requests.get(f"{SERVER}/api/price/{market}/{symbol}", headers=AUTH, timeout=10)
        data = r.json()
        return float(data.get("price") or data.get("last") or 0) or None
    except Exception:
        return None


def ask_llm(intel: dict, positions: list) -> dict | None:
    positions_summary = json.dumps(positions[:5], indent=2) if positions else "none"
    market_summary = json.dumps(intel, indent=2)[:3000]  # keep prompt small

    prompt = f"""You are an autonomous paper trading agent with $100,000 starting capital.

Current open positions:
{positions_summary}

Market intelligence:
{market_summary}

Decide ONE action to take right now, or hold.

Respond with ONLY valid JSON in exactly this format:
{{
  "action": "buy" | "sell" | "short" | "cover" | "hold",
  "symbol": "BTCUSDT",
  "market": "crypto",
  "quantity": 0.01,
  "reason": "one sentence explanation"
}}

Rules:
- action "hold" means no trade. Still return JSON with action=hold.
- market must be one of: crypto, us-stock, polymarket
- quantity must be a positive number
- For crypto: symbol like BTC, ETH. For stocks: like AAPL, TSLA.
- Never risk more than ${MAX_POSITION_USD} per trade.
- Only sell/cover if you actually have an open position in that symbol.
"""

    try:
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 256,
            },
            timeout=120,
        )
        content = r.json()["choices"][0]["message"]["content"].strip()
        # strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content)
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return None


def execute_trade(decision: dict, price: float) -> bool:
    payload = {
        "market": decision["market"],
        "action": decision["action"],
        "symbol": decision["symbol"],
        "price": price,
        "quantity": decision["quantity"],
        "content": decision.get("reason", ""),
        "executed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        r = requests.post(f"{SERVER}/api/signals/realtime", headers={**AUTH, "Content-Type": "application/json"}, json=payload, timeout=15)
        if r.status_code in (200, 201):
            print(f"[TRADE] {decision['action'].upper()} {decision['quantity']} {decision['symbol']} @ ${price:.4f}")
            return True
        else:
            print(f"[TRADE] Failed {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"[TRADE] Error: {e}")
        return False


def run():
    if not AGENT_TOKEN or AGENT_TOKEN == "YOUR_TOKEN_HERE":
        print("ERROR: Set AGENT_TOKEN in .env (copy from UI bottom-left)")
        return

    print(f"Agent started | model={LLM_MODEL} | server={SERVER} | interval={TRADE_INTERVAL_SECONDS}s")

    while True:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Researching market...")
        intel = fetch_market_intel()
        positions = fetch_my_positions()

        decision = ask_llm(intel, positions)
        if not decision:
            print("[SKIP] No decision from LLM")
        elif decision.get("action") == "hold":
            print(f"[HOLD] {decision.get('reason', '')}")
        else:
            symbol = decision.get("symbol", "BTC")
            market = decision.get("market", "crypto")
            price = fetch_price(symbol, market)
            if not price:
                print(f"[SKIP] Can't fetch price for {symbol}")
            else:
                execute_trade(decision, price)

        print(f"[SLEEP] Next check in {TRADE_INTERVAL_SECONDS}s...")
        time.sleep(TRADE_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
