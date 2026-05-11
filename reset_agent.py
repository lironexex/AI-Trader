"""
Resets agent to clean state: $100,000 cash, no positions, no trade history.
Run ONCE before a fresh challenge run.
"""

import os
import sys
import sqlite3
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "service", "server", "service", "server", "data", "clawtrader.db")
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")

if not AGENT_TOKEN or AGENT_TOKEN == "YOUR_TOKEN_HERE":
    print("ERROR: Set AGENT_TOKEN in .env")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Find agent
cur.execute("SELECT id, name, cash FROM agents WHERE token = ?", (AGENT_TOKEN,))
agent = cur.fetchone()
if not agent:
    print("ERROR: Agent not found for that token")
    sys.exit(1)

agent_id = agent["id"]
print(f"Agent: {agent['name']} (id={agent_id})")
print(f"Current cash: ${agent['cash']:,.2f}")

cur.execute("SELECT COUNT(*) as c FROM positions WHERE agent_id = ?", (agent_id,))
pos_count = cur.fetchone()["c"]
print(f"Open positions: {pos_count}")

cur.execute("SELECT COUNT(*) as c FROM signals WHERE agent_id = ?", (agent_id,))
sig_count = cur.fetchone()["c"]
print(f"Trade signals: {sig_count}")

confirm = input("\nReset to $100,000 and wipe all positions + trade history? (yes/no): ")
if confirm.strip().lower() != "yes":
    print("Aborted.")
    sys.exit(0)

cur.execute("UPDATE agents SET cash = 100000.0, deposited = 0.0 WHERE id = ?", (agent_id,))
cur.execute("DELETE FROM positions WHERE agent_id = ?", (agent_id,))
cur.execute("DELETE FROM signals WHERE agent_id = ?", (agent_id,))
cur.execute("DELETE FROM profit_history WHERE agent_id = ?", (agent_id,))
conn.commit()
conn.close()

print(f"\nDone. {agent['name']} reset to $100,000 with clean slate.")
