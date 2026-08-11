"""
webhook_relay.py — принимает алерты трёх индикаторов TradingView, пропускает
в Telegram только по одной сделке на монету и сообщает результат.

    Turtle Zone / Pullback / Grimes  ──POST JSON──>  этот сервис  ──>  Telegram

ПОЧЕМУ ГЕЙТ ЖИВЁТ ЗДЕСЬ, А НЕ В ИНДИКАТОРАХ
Три алерта TradingView друг друга не видят: каждый работает на своём графике
и ничего не знает про остальные. Правило «одна сделка на монету» может
выполнить только тот, кто видит все три потока сразу — то есть сервер.

Каждый индикатор ведёт СВОЮ сделку и шлёт два события: `entry` при входе и
`exit` с результатом WIN/LOSS при срабатывании стопа или цели. Сервер:

  entry  -> символ свободен?  да: открываем и пишем в Telegram
                              нет: молча игнорируем
  exit   -> открытая сделка по символу от ЭТОГО индикатора?
                              да: закрываем и пишем WIN/LOSS
                              нет: игнорируем — значит его вход мы не пропускали

Так поток сообщений ровно такой, как просили: сигнал -> ждём -> результат.
Ни одного повторного сообщения по монете, пока сделка не отработала.

ХРАНЕНИЕ. Состояние лежит в Neon (та же база, что у статистики бота, но в
отдельной таблице relay_trades). Render на бесплатном тарифе усыпляет сервис
и перезапускает его при каждом деплое — без базы открытые сделки терялись бы
и гейт бы протекал. Если DATABASE_URL не задан, сервис работает на памяти и
честно пишет об этом в лог.

Переменные окружения:
    TELEGRAM_BOT_TOKEN   обязательно
    TELEGRAM_CHAT_ID     обязательно
    WEBHOOK_SECRET       обязательно — тот же, что в настройках индикаторов
    DATABASE_URL         желательно  — Neon, чтобы пережить рестарт
    ACCOUNT_SIZE         по умолчанию 1000
    RISK_PCT             по умолчанию 0.02
    LEVERAGE             по умолчанию 5
    PORT                 подставляет Render

БЕЗОПАСНОСТЬ. Адрес вебхука публичный. Без WEBHOOK_SECRET сервис не стартует —
лучше не подняться, чем стоять открытым. Секрет сравнивается через
hmac.compare_digest, тело запроса ограничено 8 КБ.
"""

import hmac
import json
import logging
import os
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("relay")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PORT = int(os.environ.get("PORT", 8080))

ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "1000"))
RISK_PCT = float(os.environ.get("RISK_PCT", "0.02"))
LEVERAGE = float(os.environ.get("LEVERAGE", "5"))

MAX_BODY = 8 * 1024

_lock = threading.Lock()      # запросы приходят в потоках, состояние общее
_open: dict = {}              # symbol -> сделка (зеркало базы)
_score: dict = {}             # indicator -> [wins, losses]


# ════════════════════════════════════════════════════════════════════════════
# База
# ════════════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_trades (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    indicator   TEXT NOT NULL,
    tf          TEXT,
    direction   TEXT NOT NULL,
    entry       DOUBLE PRECISION,
    stop        DOUBLE PRECISION,
    target      DOUBLE PRECISION,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    opened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_one_open_per_symbol
    ON relay_trades (symbol) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_relay_status ON relay_trades (status);
"""


def _connect():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:
        logger.error("Postgres недоступен (%s) — работаем на памяти", type(e).__name__)
        return None


def db_exec(sql: str, args: tuple = (), fetch: bool = False):
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if fetch else None
    except Exception as e:
        logger.error("Ошибка запроса к базе: %s", e)
        return None
    finally:
        conn.close()


def db_init():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — открытые сделки будут теряться "
                       "при каждом перезапуске Render.")
        return
    db_exec(SCHEMA)
    rows = db_exec("SELECT symbol, indicator, tf, direction, entry, stop, target "
                   "FROM relay_trades WHERE status = 'OPEN'", fetch=True) or []
    for r in rows:
        _open[r[0]] = {"symbol": r[0], "indicator": r[1], "tf": r[2],
                       "dir": r[3], "entry": r[4], "stop": r[5], "target": r[6]}
    rows = db_exec("SELECT indicator, status, count(*) FROM relay_trades "
                   "WHERE status IN ('WIN','LOSS') GROUP BY 1,2", fetch=True) or []
    for ind, st, n in rows:
        _score.setdefault(ind, [0, 0])
        _score[ind][0 if st == "WIN" else 1] += n
    logger.info("Восстановлено из базы: %d открытых сделок (%s)",
                len(_open), ", ".join(sorted(_open)) or "—")


# ════════════════════════════════════════════════════════════════════════════
# Telegram
# ════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"),
                                    timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        logger.error("Telegram недоступен: %s", type(e).__name__)
        return False


def _f(x):
    """Разрядность под цену: PEPE и BTC нельзя печатать одинаково."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    return f"{v:,.8f}" if a < 0.001 else (f"{v:,.6f}" if a < 1 else f"{v:,.4f}")


def fmt_entry(t: dict) -> str:
    is_long = t["dir"] == "LONG"
    head = [
        f"{'🟢' if is_long else '🔴'} <b>{t['dir']} — {t['symbol']}</b> ({t.get('tf') or '?'})",
        f"Индикатор: {t['indicator']}",
        "",
        f"Вход:  <b>{_f(t['entry'])}</b>",
        f"Стоп:  {_f(t['stop'])}",
        f"Цель:  {_f(t['target'])}",
    ]
    if t.get("mmo"):
        head.append(f"MMO:   {_f(t['mmo'])}")

    try:
        entry, stop = float(t["entry"]), float(t["stop"])
        dist = abs(entry - stop) / entry
        if dist > 0:
            risk_usd = ACCOUNT_SIZE * RISK_PCT
            notional = min(risk_usd / dist, ACCOUNT_SIZE * LEVERAGE)
            head += [
                "",
                "💰 <b>ПОЗИЦИЯ</b>",
                f"Объём:  <b>{notional / entry:,.4f}</b>  (~${notional:,.0f} номинал)",
                f"Маржа:  <b>${notional / LEVERAGE:,.0f}</b>  при {LEVERAGE:.0f}x",
                f"Риск:   <b>${notional * dist:,.2f}</b>  ({dist * 100:.2f}% до стопа)",
            ]
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return "\n".join(head)


def fmt_exit(t: dict, result: str) -> str:
    win = result == "WIN"
    entry = float(t["entry"]) if t.get("entry") else None
    exit_px = float(t["target"]) if win else float(t["stop"])
    pnl = ""
    if entry:
        p = (exit_px - entry) / entry * 100
        if t["dir"] == "SHORT":
            p = -p
        pnl = f"  ({p:+.2f}%)"
    w, l = _score.get(t["indicator"], [0, 0])
    total = w + l
    rate = f"{w / total * 100:.0f}%" if total else "—"
    return (
        f"{'✅' if win else '🛑'} <b>{result} — {t['symbol']}</b> ({t.get('tf') or '?'})\n"
        f"{t['indicator']}  {t['dir']}\n"
        f"{_f(entry)} → {_f(exit_px)}{pnl}\n"
        f"Счёт {t['indicator']}: {w}W / {l}L  ({rate})"
    )


# ════════════════════════════════════════════════════════════════════════════
# Логика
# ════════════════════════════════════════════════════════════════════════════

def handle_entry(d: dict) -> str:
    symbol = d.get("symbol", "")
    with _lock:
        busy = _open.get(symbol)
        if busy:
            logger.info("Вход %s от %s пропущен — уже открыта сделка от %s",
                        symbol, d.get("indicator"), busy["indicator"])
            return "symbol busy"
        trade = {k: d.get(k) for k in ("symbol", "indicator", "tf", "dir",
                                        "entry", "stop", "target", "mmo")}
        _open[symbol] = trade

    db_exec("INSERT INTO relay_trades (symbol, indicator, tf, direction, entry, stop, target) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (symbol, trade["indicator"], trade["tf"], trade["dir"],
             trade["entry"], trade["stop"], trade["target"]))

    send_telegram(fmt_entry(trade))
    logger.info("ОТКРЫТА %s %s от %s", symbol, trade["dir"], trade["indicator"])
    return "opened"


def handle_exit(d: dict) -> str:
    symbol = d.get("symbol", "")
    indicator = d.get("indicator", "")
    result = d.get("result", "")
    if result not in ("WIN", "LOSS"):
        return "bad result"

    with _lock:
        trade = _open.get(symbol)
        if not trade:
            logger.info("Выход %s от %s пропущен — открытой сделки нет", symbol, indicator)
            return "no open trade"
        if trade["indicator"] != indicator:
            logger.info("Выход %s от %s пропущен — сделку открывал %s",
                        symbol, indicator, trade["indicator"])
            return "other indicator"
        del _open[symbol]
        _score.setdefault(indicator, [0, 0])
        _score[indicator][0 if result == "WIN" else 1] += 1

    db_exec("UPDATE relay_trades SET status = %s, closed_at = now() "
            "WHERE symbol = %s AND status = 'OPEN'", (result, symbol))

    send_telegram(fmt_exit(trade, result))
    logger.info("ЗАКРЫТА %s %s -> %s", symbol, indicator, result)
    return "closed"


# ════════════════════════════════════════════════════════════════════════════
# HTTP
# ════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: str = "ok"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Health-check для Render и пинга UptimeRobot; заодно видно состояние.
        with _lock:
            state = ", ".join(f"{s}:{t['indicator']}" for s, t in sorted(_open.items()))
        self._reply(200, f"alive | открыто {len(_open)}: {state or '—'}")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._reply(400, "bad length")
            return
        if length <= 0 or length > MAX_BODY:
            self._reply(400, "bad length")
            return

        try:
            d = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(d, dict):
                raise ValueError
        except Exception:
            logger.warning("Отброшено: тело не JSON")
            self._reply(400, "expected json")
            return

        if not hmac.compare_digest(str(d.get("secret", "")), SECRET):
            logger.warning("Отброшено: неверный секрет")
            self._reply(403, "forbidden")
            return

        event = d.get("event", "entry")
        try:
            if event == "entry":
                self._reply(200, handle_entry(d))
            elif event == "exit":
                self._reply(200, handle_exit(d))
            else:
                self._reply(400, "unknown event")
        except Exception as e:
            logger.error("Ошибка обработки: %s", e, exc_info=True)
            self._reply(500, "error")

    def log_message(self, *args):
        pass


def main():
    if not BOT_TOKEN or not CHAT_ID:
        logger.critical("TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID обязательны.")
        sys.exit(1)
    if not SECRET:
        logger.critical("WEBHOOK_SECRET не задан. Без него адрес вебхука открыт "
                        "для кого угодно — сервис намеренно не стартует.")
        sys.exit(1)

    db_init()
    logger.info("=" * 56)
    logger.info("  TradingView -> Telegram relay")
    logger.info("  Порт: %s   База: %s", PORT, "Neon" if DATABASE_URL else "нет (память)")
    logger.info("  Депозит $%.0f, риск %.0f%%, плечо %.0fx",
                ACCOUNT_SIZE, RISK_PCT * 100, LEVERAGE)
    logger.info("=" * 56)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
