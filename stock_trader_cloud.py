# AI Stock Trader V10 — educational paper trading only
# Install in Pydroid 3: pip install requests
import os
import json
import time
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL = "AAPL"
START_CASH = 1000.0
CHECK_SECONDS = 300
STOP_LOSS_PCT = 0.04
TAKE_PROFIT_PCT = 0.08
MAX_POSITION_PCT = 0.70
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_trader_v10_portfolio.json")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Android; Mobile)"})
NY = ZoneInfo("America/New_York")

def send(message):
    try:
        r = session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                         data={"chat_id": CHAT_ID, "text": message}, timeout=20)
        r.raise_for_status()
        if not r.json().get("ok"):
            print("Telegram:", r.text[:250])
    except Exception as exc:
        print("Telegram error:", exc)

def initial_state():
    return {"cash": START_CASH, "shares": 0, "entry": 0.0,
            "realized": 0.0, "history": [], "enabled": True,
            "last_signal": "", "last_price": 0.0, "last_quote": "",
            "last_reason": "", "last_update_id": 0}

def load():
    if not os.path.isfile(DATA_FILE):
        return initial_state()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        state = initial_state()
        state.update(saved)
        return state
    except Exception as exc:
        raise RuntimeError("Не удалось прочитать портфель. Файл не перезаписан: " + str(exc))

def save(state):
    temp = DATA_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp, DATA_FILE)

def get_market():
    # Yahoo's free chart endpoint is unofficial and may be unavailable/delayed.
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    r = session.get(url, params={"interval": "5m", "range": "5d"}, timeout=20)
    r.raise_for_status()
    obj = r.json()["chart"]
    if obj.get("error") or not obj.get("result"):
        raise ValueError("Нет котировок: " + str(obj.get("error")))
    result = obj["result"][0]
    ts = result.get("timestamp") or []
    quotes = result["indicators"]["quote"][0]["close"]
    candles = [(int(t), float(p)) for t, p in zip(ts, quotes)
               if p is not None and math.isfinite(float(p)) and float(p) > 0]
    if len(candles) < 55:
        raise ValueError("Недостаточно данных для SMA50 и RSI14")
    last_ts, price = candles[-1]
    prices = [p for _, p in candles]
    sma20 = sum(prices[-20:]) / 20
    sma50 = sum(prices[-50:]) / 50
    changes = [prices[i] - prices[i-1] for i in range(len(prices)-14, len(prices))]
    gains = sum(max(x, 0) for x in changes) / 14
    losses = sum(max(-x, 0) for x in changes) / 14
    rsi = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    return {"price": price, "ts": last_ts, "sma20": sma20, "sma50": sma50, "rsi": rsi}

def market_open(now):
    # Weekday/time gate; exchange holidays are NOT verified.
    local = now.astimezone(NY)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 570 <= minutes < 960

def evaluate(data, now):
    age = (now - datetime.fromtimestamp(data["ts"], timezone.utc)).total_seconds()
    if age < -120 or age > 900:
        return "PAUSE", "Котировка устарела или имеет неверное время"
    if not market_open(now):
        return "PAUSE", "Вне обычных часов NYSE (праздники не проверяются)"
    if data["price"] > data["sma20"] > data["sma50"] and 50 <= data["rsi"] <= 70:
        return "BUY", "Цена выше SMA20/SMA50, RSI 50–70"
    if data["price"] < data["sma20"] or data["rsi"] >= 75:
        return "SELL", "Цена ниже SMA20 или RSI >= 75"
    return "WAIT", "Условий для входа нет"

def portfolio_value(state):
    return state["cash"] + state["shares"] * state["last_price"]

def portfolio_text(state):
    value = portfolio_value(state)
    pnl = value - START_CASH
    return ("💼 Виртуальный портфель V10\n"
            f"💵 Деньги: ${state['cash']:.2f}\n"
            f"📦 {SYMBOL}: {state['shares']} шт.\n"
            f"📈 Портфель: ${value:.2f}\n"
            f"💰 Результат: ${pnl:+.2f} ({pnl/START_CASH*100:+.2f}%)\n"
            f"Закрытые сделки: ${state['realized']:+.2f}\n"
            f"Вход: ${state['entry']:.2f}\n"
            f"SL: ${state['entry']*(1-STOP_LOSS_PCT):.2f} | TP: ${state['entry']*(1+TAKE_PROFIT_PCT):.2f}\n"
            f"Режим: {'▶️ активен' if state['enabled'] else '⏸ пауза'}\n"
            "⚠️ Только учебная симуляция")

def status_text(state):
    return (f"🤖 V10 | {SYMBOL}\n"
            f"Цена: ${state['last_price']:.2f}\n"
            f"Сигнал: {state['last_signal'] or 'нет'}\n"
            f"Причина: {state['last_reason'] or 'ожидание'}\n"
            f"Котировка UTC: {state['last_quote'] or 'нет'}\n"
            f"Сделки: {'включены' if state['enabled'] else 'на паузе'}\n"
            "⚠️ Бесплатные данные могут задерживаться")

def history_text(state):
    rows = state["history"][-8:]
    if not rows:
        return "📜 История V10 пока пуста. Старые V6–V9 сделки не импортируются."
    return "📜 Последние сделки V10:\n" + "\n".join(rows)

def process_commands(state):
    # Only messages from the configured private chat are accepted.
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    r = session.get(url, params={"offset": state["last_update_id"]+1, "timeout": 5}, timeout=12)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise ValueError(str(payload))
    for item in payload.get("result", []):
        state["last_update_id"] = max(state["last_update_id"], item["update_id"])
        save(state)
        message = item.get("message", {})
        if str(message.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        command = (message.get("text") or "").strip().split(" ")[0].split("@")[0].lower()
        if command == "/balance":
            send(portfolio_text(state))
        elif command == "/status":
            send(status_text(state))
        elif command == "/history":
            send(history_text(state))
        elif command == "/report":
            send(portfolio_text(state) + "\n\n" + status_text(state))
        elif command == "/pause":
            state["enabled"] = False
            save(state)
            send("⏸ Новые виртуальные сделки остановлены. /resume — продолжить. Открытая позиция остаётся без автоматической защиты, пока пауза включена.")
        elif command == "/resume":
            state["enabled"] = True
            save(state)
            send("▶️ Виртуальная торговля включена. Проверка при следующем обновлении.")
        elif command in ("/help", "/start"):
            send("🤖 V10 команды: /balance /status /history /report /pause /resume /help\n⚠️ Только виртуальная торговля.")

def run_check(state):
    now = datetime.now(timezone.utc)
    data = get_market()
    state["last_price"] = data["price"]
    state["last_quote"] = datetime.fromtimestamp(data["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    signal, reason = evaluate(data, now)
    state["last_reason"] = reason
    old_signal = state["last_signal"]
    state["last_signal"] = signal
    save(state)
    print(now.strftime("%H:%M:%S"), SYMBOL, f"${data['price']:.2f}", signal, reason)
    if signal != old_signal:
        send(f"📊 V10 | {SYMBOL} ${data['price']:.2f}\nСигнал: {signal}\nПричина: {reason}\nКотировка: {state['last_quote']}")
    if not state["enabled"] or signal == "PAUSE":
        return
    price = data["price"]
    if state["shares"]:
        entry = state["entry"]
        sl = entry * (1-STOP_LOSS_PCT)
        tp = entry * (1+TAKE_PROFIT_PCT)
        exit_reason = ""
        if price <= sl:
            exit_reason = "STOP LOSS"
        elif price >= tp:
            exit_reason = "TAKE PROFIT"
        elif signal == "SELL":
            exit_reason = "SELL сигнал"
        if exit_reason:
            qty = state["shares"]
            profit = (price-entry)*qty
            state["cash"] = round(state["cash"]+qty*price, 6)
            state["realized"] = round(state["realized"]+profit, 6)
            state["shares"] = 0
            state["entry"] = 0.0
            row = f"{now:%Y-%m-%d %H:%M} UTC | SELL {qty} @ ${price:.2f} | {exit_reason} | P/L ${profit:+.2f}"
            state["history"].append(row)
            save(state)
            send("🔴 ВИРТУАЛЬНАЯ ПРОДАЖА\n" + row + "\n" + portfolio_text(state))
    elif signal == "BUY":
        qty = int(min(state["cash"]*MAX_POSITION_PCT, state["cash"]) // price)
        if qty > 0:
            state["cash"] = round(state["cash"]-qty*price, 6)
            state["shares"] = qty
            state["entry"] = price
            row = f"{now:%Y-%m-%d %H:%M} UTC | BUY {qty} @ ${price:.2f}"
            state["history"].append(row)
            save(state)
            send("🟢 ВИРТУАЛЬНАЯ ПОКУПКА\n" + row + "\n" + portfolio_text(state))

def main():
    if not TOKEN.strip() or not CHAT_ID.strip():
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secrets")
    state = load()
    # One scheduled run: process queued Telegram commands and check market once.
    # Only one GitHub Actions job at a time; state is committed after every run.
    try:
        process_commands(state)
    except Exception as exc:
        print("Ошибка команд:", exc)
    try:
        run_check(state)
    except Exception as exc:
        print("Ошибка котировок:", exc)
        if state["last_signal"] != "ERROR":
            state["last_signal"] = "ERROR"
            state["last_reason"] = "Не удалось обновить котировки"
            save(state)
            send("⚠️ V10 Cloud: котировки недоступны. Сделки не выполняются.")

if __name__ == "__main__":
    main()
