"""AI Stock Trader V11 Cloud: EDUCATIONAL paper trades only, no broker orders."""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SYMBOLS = ("AAPL", "NVDA", "TSLA", "KO", "MSFT")
START_CASH = 1000.0
MAX_PER_SYMBOL = 0.20
MAX_INVESTED = 0.80
STOP_LOSS = 0.04
TAKE_PROFIT = 0.08
MAX_QUOTE_AGE_SECONDS = 15 * 60
FILE = Path(__file__).with_name("v11_portfolio.json")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NY = ZoneInfo("America/New_York")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; EducationalPaperTrader/1.0)"})


def fresh_state():
    return {
        "version": 11, "cash": START_CASH, "positions": {}, "history": [],
        "enabled": True, "last_update_id": 0, "last_signals": {},
        "last_trade_candle": {}, "last_quotes": {}, "created_utc": datetime.now(timezone.utc).isoformat(),
    }


def load_state():
    if not FILE.exists():
        return fresh_state()
    with FILE.open(encoding="utf-8") as f:
        state = json.load(f)
    if state.get("version") != 11 or not isinstance(state.get("positions"), dict):
        raise RuntimeError("Некорректный формат v11_portfolio.json. Не перезаписываю файл.")
    for key, default in fresh_state().items():
        state.setdefault(key, default)
    return state


def save_state(state):
    temp = FILE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    temp.replace(FILE)


def telegram_api(method, **kwargs):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID GitHub Actions secrets")
    resp = SESSION.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=kwargs, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} returned error: {payload.get('description', 'unknown')}")
    return payload["result"]


def send(text):
    telegram_api("sendMessage", chat_id=CHAT_ID, text=text[:4000])


def get_market(symbol):
    # Yahoo Finance chart API is unofficial; quotes can be unavailable/delayed.
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    response = SESSION.get(url, params={"interval": "5m", "range": "5d", "includePrePost": "false"}, timeout=25)
    response.raise_for_status()
    chart = response.json()["chart"]
    if chart.get("error") or not chart.get("result"):
        raise RuntimeError(f"Market feed error for {symbol}: {chart.get('error')}")
    result = chart["result"][0]
    stamps = result.get("timestamp") or []
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    candles = []
    for stamp, value in zip(stamps, closes):
        if value is not None and math.isfinite(float(value)) and float(value) > 0:
            candles.append((int(stamp), float(value)))
    if len(candles) < 51:
        raise RuntimeError(f"Not enough valid 5-minute candles for {symbol}")
    prices = [v for _, v in candles]
    changes = [prices[i] - prices[i-1] for i in range(len(prices)-14, len(prices))]
    avg_gain = sum(max(d, 0) for d in changes) / 14
    avg_loss = sum(max(-d, 0) for d in changes) / 14
    rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return {"price": prices[-1], "ts": candles[-1][0],
            "sma20": sum(prices[-20:]) / 20, "sma50": sum(prices[-50:]) / 50,
            "rsi": rsi}


def session_open(now):
    local = now.astimezone(NY)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 570 <= minute < 960


def signal(market, now):
    age = (now - datetime.fromtimestamp(market["ts"], timezone.utc)).total_seconds()
    if age < -120 or age > MAX_QUOTE_AGE_SECONDS:
        return "PAUSE", "Котировка устарела или имеет неверное время"
    if not session_open(now):
        return "PAUSE", "NYSE вне обычных часов (праздники не проверяются)"
    if market["price"] > market["sma20"] > market["sma50"] and 50 <= market["rsi"] <= 70:
        return "BUY", "SMA20 > SMA50, RSI 50–70"
    if market["price"] < market["sma20"] or market["rsi"] >= 75:
        return "SELL", "Цена ниже SMA20 или RSI >= 75"
    return "WAIT", "Условий для сделки нет"


def value(state):
    invested = 0.0
    for symbol, position in state["positions"].items():
        quote = state["last_quotes"].get(symbol, {})
        price = float(quote.get("price", position["entry"]))
        invested += float(position["qty"]) * price
    return state["cash"] + invested


def portfolio_message(state):
    lines = ["💼 V11 | Виртуальный портфель", f"💵 Наличные: ${state['cash']:.2f}",
             f"📊 Оценка: ${value(state):.2f}", f"📈 P/L: ${value(state)-START_CASH:+.2f}",
             f"Режим: {'активен' if state['enabled'] else 'пауза'}", "Позиции:"]
    if not state["positions"]:
        lines.append("Нет открытых позиций")
    for symbol, position in sorted(state["positions"].items()):
        lines.append(f"{symbol}: {position['qty']:.4f} шт. по ${position['entry']:.2f} | SL ${position['entry']*(1-STOP_LOSS):.2f} | TP ${position['entry']*(1+TAKE_PROFIT):.2f}")
    lines.append("⚠️ Только виртуальные сделки; котировки могут задерживаться")
    return "\n".join(lines)


def process_commands(state):
    # Poll only once per workflow run. Do not run Pydroid instance concurrently.
    offset = int(state.get("last_update_id", 0)) + 1
    updates = telegram_api("getUpdates", offset=offset, timeout=0, limit=100, allowed_updates=json.dumps(["message"]))
    for update in updates:
        uid = int(update["update_id"])
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id", "")) != CHAT_ID:
            state["last_update_id"] = uid
            continue
        command = (message.get("text") or "").split(maxsplit=1)[0].split("@")[0].lower()
        if command in ("/balance", "/status", "/report"):
            send(portfolio_message(state))
        elif command == "/history":
            history = state["history"][-8:]
            send("📒 V11 последние операции:\n" + ("\n".join(
                f"{h['time']} {h['side']} {h['symbol']} {h['qty']:.4f} × ${h['price']:.2f}" for h in history
            ) if history else "Пока нет сделок"))
        elif command == "/pause":
            state["enabled"] = False
            send("⏸ V11: новые виртуальные сделки остановлены")
        elif command == "/resume":
            state["enabled"] = True
            send("▶️ V11: виртуальная торговля включена")
        elif command in ("/help", "/start"):
            send("🤖 V11 Multi-Stock: AAPL NVDA TSLA KO MSFT\n/balance /status /report /history /pause /resume /help\nКоманды обрабатываются при следующем запуске GitHub Actions.")
        state["last_update_id"] = uid
        save_state(state)


def trade(state, symbol, side, qty, price, timestamp, reason):
    amount = round(qty * price, 8)
    if side == "BUY":
        state["cash"] = round(state["cash"] - amount, 8)
        state["positions"][symbol] = {"qty": qty, "entry": price}
    else:
        state["cash"] = round(state["cash"] + amount, 8)
        del state["positions"][symbol]
    state["history"].append({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "symbol": symbol, "side": side, "qty": qty, "price": price, "reason": reason})
    state["last_trade_candle"][symbol] = timestamp
    save_state(state)  # Persist before sending; avoid replay on notification failure.
    send(f"{'🟢' if side == 'BUY' else '🔴'} V11 {side} {symbol}\n{qty:.4f} виртуальных акций × ${price:.2f}\n{reason}\n💵 Наличные ${state['cash']:.2f}")


def run():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in repository secrets")
    state = load_state()
    process_commands(state)
    now = datetime.now(timezone.utc)
    summary = []
    errors = []
    for symbol in SYMBOLS:
        try:
            market = get_market(symbol)
            price = market["price"]
            state["last_quotes"][symbol] = {"price": price, "ts": market["ts"]}
            action, reason = signal(market, now)
            summary.append(f"{symbol} ${price:.2f} {action}")
            position = state["positions"].get(symbol)
            # No virtual fills on stale quotes or when paused.
            if action == "PAUSE" or not state["enabled"]:
                continue
            if state["last_trade_candle"].get(symbol) == market["ts"]:
                continue
            if position:
                entry = position["entry"]
                if price <= entry * (1 - STOP_LOSS):
                    trade(state, symbol, "SELL", position["qty"], price, market["ts"], "Stop Loss 4% (по проверенной цене)")
                elif price >= entry * (1 + TAKE_PROFIT):
                    trade(state, symbol, "SELL", position["qty"], price, market["ts"], "Take Profit 8% (по проверенной цене)")
                elif action == "SELL":
                    trade(state, symbol, "SELL", position["qty"], price, market["ts"], reason)
            elif action == "BUY":
                equity = value(state)
                invested = equity - state["cash"]
                budget = min(MAX_PER_SYMBOL * equity, MAX_INVESTED * equity - invested, state["cash"])
                if budget >= 1:
                    # Fractional shares exist ONLY in this educational simulation.
                    qty = round(budget / price, 6)
                    if qty > 0 and qty * price <= state["cash"] + 1e-7:
                        trade(state, symbol, "BUY", qty, price, market["ts"], reason)
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            print(f"ERROR {symbol}: {type(exc).__name__}: {exc}", flush=True)
    save_state(state)
    print("V11 CHECK " + " | ".join(summary), flush=True)
    if errors:
        # Explicitly fail so Github Success never conceals a partial market failure.
        raise RuntimeError("Market errors: " + "; ".join(errors))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"V11 FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
