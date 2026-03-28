"""
SMC TM Supreme → Dhan Auto-Trading Bot
======================================================
Listens for TradingView webhook alerts (JSON) and
places orders on Dhan API automatically.

Setup:
  1. pip install flask dhanhq python-dotenv
  2. Create .env file with your Dhan credentials
  3. Run: python dhan_bot.py
  4. Expose publicly: ngrok http 5000
  5. Paste ngrok URL in TradingView alert Webhook URL field

Alert message format from Pine Script:
  {"action":"BUY","symbol":"NIFTY","ep":266.1,"sl":261.1,"tp1":268.6,...}
"""

import os
import json
import logging
from flask import Flask, request, jsonify
from dhanhq import dhanhq, DhanContext
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── Dhan Credentials (set in .env file) ────────────────────
CLIENT_ID    = os.getenv("DHAN_CLIENT_ID")
ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

# ─── Trade Configuration ─────────────────────────────────────
# Map of symbol (from TradingView) → Dhan Security ID
SECURITY_MAP = {
    "NIFTY":    "13",
    "BANKNIFTY":"25",
    "RELIANCE": "2885",
    "ADANIPOWER": "11506",  # ADANIPOWER NSE EQ Security ID
}

USE_DYNAMIC_QTY = True      # Calculate QTY from balance
CAPITAL_PCT     = 1.0       # 100% of available balance
LEVERAGE        = 5.0       # 5x Intraday Leverage for Equities

DEFAULT_QTY = 1             # Fallback if balance fetch fails
PRODUCT     = "INTRADAY"    # INTRADAY or CNC or MIS
EXCHANGE    = "NSE_EQ"      # NSE_EQ, NSE_FNO, BSE_EQ etc.

# ─── State Tracking ──────────────────────────────────────────
# Track open position per symbol to avoid duplicate orders
positions = {}   # {symbol: {"side": "BUY"/"SELL", "order_id": "...", "qty": 100}}

# ─── Dhan Client ─────────────────────────────────────────────
c = DhanContext(CLIENT_ID, ACCESS_TOKEN)
dhan = dhanhq(c)

app = Flask(__name__)


def get_security_id(symbol: str):
    """Return Dhan security_id for the given symbol."""
    # Sometimes TV sends NSE:ADANIPOWER, we safely strip exchange prefix
    clean_sym = symbol.split(":")[-1].upper()
    sid = SECURITY_MAP.get(clean_sym)
    if not sid:
        log.error(f"Unknown symbol: {clean_sym}. Add it to SECURITY_MAP.")
    return sid


def get_quantity(price):
    """Fetch balance and compute quantity using Leverage."""
    if not USE_DYNAMIC_QTY or price <= 0:
        return DEFAULT_QTY
        
    try:
        limits = dhan.get_fund_limits()
        # Parse available balance from response (structure relies on SDK version)
        if "data" in limits and isinstance(limits["data"], dict):
            balance = float(limits["data"].get("availabelBalance", 0))
        elif isinstance(limits, dict):
            balance = float(limits.get("availabelBalance", limits.get("marginAvailable", 0)))
        else:
            balance = 0.0
            
        if balance <= 0:
            log.warning("Zero or missing balance. Using DEFAULT_QTY.")
            return DEFAULT_QTY

        # Capital formula: (Balance * Capacity_pct * Leverage) / Stock Price
        buying_power = balance * CAPITAL_PCT * LEVERAGE
        qty = int(buying_power / price)
        log.info(f"CALC QTY: Balance={balance:.2f} | BuyingPower={buying_power:.2f} | Price={price} -> QTY={qty}")
        return max(1, qty)
    except Exception as e:
        log.error(f"Failed to calculate dynamic quantity: {e}")
        return DEFAULT_QTY


def place_market_order(symbol, side, price=0, forced_qty=None):
    """Place a market order. side = 'BUY' or 'SELL'."""
    security_id = get_security_id(symbol)
    if not security_id:
        return None

    transaction = dhan.BUY if side == "BUY" else dhan.SELL
    qty = forced_qty if forced_qty else get_quantity(price)

    try:
        resp = dhan.place_order(
            security_id=security_id,
            exchange_segment=EXCHANGE,
            transaction_type=transaction,
            quantity=qty,
            order_type=dhan.MARKET,
            product_type=PRODUCT,
            price=0  # market order
        )
        log.info(f"ORDER PLACED: {side} {qty}x {symbol} | Response: {resp}")
        # Inject our calculated quantity so we can track it
        if isinstance(resp, dict) and resp.get("status") != "failure":
             resp["_computed_qty"] = qty
        return resp
    except Exception as e:
        log.error(f"ORDER FAILED: {side} {symbol} | Error: {e}")
        return None


def get_actual_position_qty(symbol):
    """Query Dhan API for the true real-time open quantity of this symbol."""
    security_id = get_security_id(symbol)
    if not security_id:
        return 0
        
    try:
        resp = dhan.get_positions()
        if isinstance(resp, dict) and resp.get("status") == "success":
            data = resp.get("data", [])
            for pos in data:
                # Match symbol and INTRADAY product type
                if str(pos.get("securityId")) == str(security_id) and pos.get("productType") == PRODUCT:
                    return int(pos.get("netQty", 0))
    except Exception as e:
        log.error(f"Failed to fetch live positions: {e}")
        
    return 0


def close_position(symbol):
    """Close any open position by querying Dhan for the EXACT live quantity."""
    actual_qty = get_actual_position_qty(symbol)
    
    if actual_qty == 0:
        log.warning(f"No live open position exists on Dhan for {symbol}. Skipping exit (likely manually closed).")
        # Clear stale memory if it exists
        if symbol in positions:
            del positions[symbol]
        return None

    # If actual_qty > 0 (LONG), we must SELL. If < 0 (SHORT), we must BUY.
    exit_side = "SELL" if actual_qty > 0 else "BUY"
    abs_qty = abs(actual_qty)
    
    log.info(f"CLOSING LIVE POSITION on {symbol} with {exit_side} {abs_qty}x")
    resp = place_market_order(symbol, exit_side, price=0, forced_qty=abs_qty)
    
    if resp and symbol in positions:
        del positions[symbol]
        
    return resp


@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.data.decode("utf-8")
    log.info(f"\n--- INCOMING ALERT ---\n{raw}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.error("Invalid JSON payload")
        return jsonify({"error": "Invalid JSON"}), 400

    action = data.get("action", "").upper()
    symbol = data.get("symbol", "").upper()
    
    # Pine Script sends Entry Price as "ep", use that for QTY calculation
    price = float(data.get("ep", data.get("price", 0))) 
    reason = data.get("reason", "N/A")

    if not action or not symbol:
        return jsonify({"error": "Missing action or symbol"}), 400

    # ── ENTRY: BUY ──────────────────────────────────────────
    if action == "BUY":
        if symbol in positions:
            log.warning(f"Already in a position for {symbol}, skipping BUY")
        else:
            resp = place_market_order(symbol, "BUY", price=price)
            if resp:
                positions[symbol] = {"side": "BUY", "order_id": resp.get("orderId"), "qty": resp.get("_computed_qty", DEFAULT_QTY)}

    # ── ENTRY: SELL ─────────────────────────────────────────
    elif action == "SELL":
        if symbol in positions:
            log.warning(f"Already in a position for {symbol}, skipping SELL")
        else:
            resp = place_market_order(symbol, "SELL", price=price)
            if resp:
                positions[symbol] = {"side": "SELL", "order_id": resp.get("orderId"), "qty": resp.get("_computed_qty", DEFAULT_QTY)}

    # ── FLIP: Close existing + open opposite ────────────────
    elif action in ("FLIP_SELL", "FLIP_BUY"):
        close_position(symbol)   # close current
        new_side = "SELL" if action == "FLIP_SELL" else "BUY"
        resp = place_market_order(symbol, new_side, price=price)
        if resp:
            positions[symbol] = {"side": new_side, "order_id": resp.get("orderId"), "qty": resp.get("_computed_qty", DEFAULT_QTY)}

    # ── FULL EXITS (SL, SafeExit, RevExit, 3PM, TP5) ───────────────
    elif action in ("EXIT", "SL_HIT", "TP5_HIT"):
        log.info(f"FULL EXIT TRIGGERED: {action} | Reason: {reason}")
        close_position(symbol)

    # ── PARTIAL TARGET HITS: Log only (Add partial exit logic if desired later) ─
    elif action in ("TP1_HIT", "TP2_HIT", "TP3_HIT", "TP4_HIT"):
        log.info(f"🎯 INTERMEDIATE TARGET: {action} for {symbol} @ {price} — Position stays open until T5, SL, or Signal Exit.")

    # ── EARLY WARNINGS (Pad Break / Micro Danger) ────────────
    elif action == "PAD_BREAK":
        side = data.get("side", "")
        log.warning(f"⚠️ PAD BREAK WARNING ({side}) on {symbol}")

    else:
        log.warning(f"Unknown action: {action}")

    return jsonify({"status": "ok", "action": action, "symbol": symbol}), 200


@app.route("/status", methods=["GET"])
def status():
    """Check bot status and open positions."""
    return jsonify({"positions": positions, "status": "running"}), 200


if __name__ == "__main__":
    log.info("Starting Dhan Auto-Trading Bot...")
    log.info(f"Open positions: {positions}")
    # Render assigns a dynamic port via the PORT environment variable
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
