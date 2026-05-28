"""
╔══════════════════════════════════════════════════════════╗
║         CRYPTO SIGNAL BOT  v2.0                          ║
║  Анализирует движение денег — не слова аналитиков        ║
╚══════════════════════════════════════════════════════════╝

ИСТОЧНИКИ ДАННЫХ:
  ── Рыночные данные ──────────────────────────────────────
  • CoinGecko         — цена, объём, доминация (бесплатно)
  • Binance Spot      — технический анализ SMA (бесплатно)

  ── Деривативы (опережающие индикаторы) ─────────────────
  • Binance Futures   — фандинг, OI, агрессивность (бесплат.)
  • Binance Futures   — Long/Short ratio (бесплатно)
  • Binance Futures   — Top traders positions (бесплатно)
  • Deribit           — Put/Call ratio, Max Pain (бесплатно)

  ── Настроение рынка ─────────────────────────────────────
  • Fear & Greed      — индекс страха/жадности (бесплатно)
  • Reddit            — настроение сообщества (бесплатно)
  • CryptoPanic       — горячие новости (бесплатно)
"""

import os
import json
import time
import schedule
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic

load_dotenv()

# ─────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
CLAUDE_API_KEY      = os.getenv("CLAUDE_API_KEY")
CRYPTOPANIC_TOKEN   = os.getenv("CRYPTOPANIC_TOKEN", "")
ANALYSIS_INTERVAL_H = int(os.getenv("ANALYSIS_INTERVAL_HOURS", "6"))

HEADERS     = {"User-Agent": "CryptoSignalBot/2.0"}
BINANCE_F   = "https://fapi.binance.com"
BINANCE_S   = "https://api.binance.com"
DERIBIT     = "https://www.deribit.com/api/v2/public"


# ═══════════════════════════════════════════
#  БЛОК 1: БАЗОВЫЕ РЫНОЧНЫЕ ДАННЫЕ
# ═══════════════════════════════════════════

def fetch_btc_price() -> dict | None:
    """Цена BTC, изменения, объём (CoinGecko)"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd"
            "&include_24hr_change=true&include_24hr_vol=true"
            "&include_market_cap=true&include_7d_change=true",
            headers=HEADERS, timeout=10
        )
        d = r.json()["bitcoin"]
        return {
            "price":        d["usd"],
            "change_24h":   round(d["usd_24h_change"], 2),
            "change_7d":    round(d.get("usd_7d_change", 0), 2),
            "volume_24h_B": round(d["usd_24h_vol"] / 1e9, 2),
            "mcap_B":       round(d["usd_market_cap"] / 1e9, 1),
        }
    except Exception as e:
        print(f"[WARN] btc_price: {e}"); return None


def fetch_technical_data() -> dict | None:
    """SMA20/50, диапазон, объём-тренд (Binance Spot)"""
    try:
        r = requests.get(
            f"{BINANCE_S}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=52",
            headers=HEADERS, timeout=10
        )
        kl = r.json()
        closes = [float(k[4]) for k in kl]
        highs  = [float(k[2]) for k in kl]
        lows   = [float(k[3]) for k in kl]
        vols   = [float(k[5]) for k in kl]
        cur = closes[-1]

        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50
        vol10 = sum(vols[-10:]) / 10
        vol30 = sum(vols[-30:]) / 30

        # RSI-14
        gains, losses = [], []
        for i in range(-15, -1):
            diff = closes[i+1] - closes[i]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_g = sum(gains) / 14 if gains else 0.001
        avg_l = sum(losses) / 14 if losses else 0.001
        rsi = round(100 - 100 / (1 + avg_g / avg_l), 1)

        return {
            "sma20":           round(sma20, 0),
            "sma50":           round(sma50, 0),
            "price_vs_sma20":  "выше ✅" if cur > sma20 else "ниже ⚠️",
            "price_vs_sma50":  "выше ✅" if cur > sma50 else "ниже ⚠️",
            "rsi14":           rsi,
            "rsi_signal":      "перекуплен 🔴" if rsi > 70 else "перепродан 🟢" if rsi < 30 else "нейтральный",
            "high_30d":        round(max(highs[-30:]), 0),
            "low_30d":         round(min(lows[-30:]), 0),
            "range_position":  round((cur - min(lows[-30:])) / (max(highs[-30:]) - min(lows[-30:])) * 100, 1),
            "volume_trend":    "растёт 📈" if vol10 > vol30 * 1.1 else "падает 📉" if vol10 < vol30 * 0.9 else "стабильный",
        }
    except Exception as e:
        print(f"[WARN] technical: {e}"); return None


# ═══════════════════════════════════════════
#  БЛОК 2: ДЕРИВАТИВЫ (опережающие сигналы)
# ═══════════════════════════════════════════

def fetch_funding_and_oi() -> dict | None:
    """Фандинг + история OI за последние 4 часа (Binance Futures)"""
    try:
        # Текущий фандинг
        r1 = requests.get(f"{BINANCE_F}/fapi/v1/premiumIndex?symbol=BTCUSDT",
                          headers=HEADERS, timeout=10)
        d = r1.json()
        funding = float(d["lastFundingRate"]) * 100

        # История OI (почасовая)
        r2 = requests.get(
            f"{BINANCE_F}/futures/data/openInterestHist"
            "?symbol=BTCUSDT&period=1h&limit=5",
            headers=HEADERS, timeout=10
        )
        oi_hist = r2.json()
        oi_values = [float(x["sumOpenInterest"]) for x in oi_hist]
        oi_now   = oi_values[-1]
        oi_4h_ago = oi_values[0]
        oi_change_pct = round((oi_now - oi_4h_ago) / oi_4h_ago * 100, 2)

        # Интерпретация OI + фандинг
        price_r = requests.get(f"{BINANCE_F}/fapi/v1/ticker/price?symbol=BTCUSDT",
                               headers=HEADERS, timeout=5)
        cur_price = float(price_r.json()["price"])
        r3 = requests.get(
            f"{BINANCE_F}/futures/data/openInterestHist"
            "?symbol=BTCUSDT&period=1h&limit=2",
            headers=HEADERS, timeout=10
        )
        oi2 = r3.json()
        price_change = 0
        signal = "нейтральный"
        if oi_change_pct > 1 and funding > 0.01:
            signal = "бычий тренд усиливается 🟢"
        elif oi_change_pct > 1 and funding < -0.01:
            signal = "медвежий тренд усиливается 🔴"
        elif oi_change_pct > 2 and abs(funding) > 0.05:
            signal = "⚠️ СКВИЗ ВОЗМОЖЕН — экстремальный OI+фандинг"
        elif oi_change_pct < -1:
            signal = "делевередж — позиции закрываются"

        return {
            "funding_rate_pct":  round(funding, 4),
            "funding_sentiment": "лонги платят шортам" if funding > 0 else "шорты платят лонгам",
            "oi_now_B":          round(oi_now / 1e9, 3),
            "oi_change_4h_pct":  oi_change_pct,
            "oi_signal":         signal,
            "squeeze_risk":      abs(funding) > 0.05 or abs(oi_change_pct) > 3,
        }
    except Exception as e:
        print(f"[WARN] funding_oi: {e}"); return None


def fetch_taker_ratio() -> dict | None:
    """Агрессивность покупателей vs продавцов (Binance Futures)

    ВАЖНО: если покупатели агрессивны → движение вверх реальное
    Если цена растёт но тейкеры продают → слабый рост, скоро разворот
    """
    try:
        r = requests.get(
            f"{BINANCE_F}/futures/data/takerlongshortRatio"
            "?symbol=BTCUSDT&period=1h&limit=4",
            headers=HEADERS, timeout=10
        )
        data = r.json()
        ratios = [float(x["buySellRatio"]) for x in data]
        current = ratios[-1]
        avg = sum(ratios) / len(ratios)
        trend = "покупатели агрессивнее" if current > avg * 1.05 else \
                "продавцы агрессивнее" if current < avg * 0.95 else "баланс"

        return {
            "current_ratio":   round(current, 3),
            "avg_4h":          round(avg, 3),
            "interpretation":  "🟢 покупатели доминируют" if current > 1.1 else
                               "🔴 продавцы доминируют" if current < 0.9 else
                               "⚪ баланс сил",
            "trend_vs_4h_avg": trend,
            "note": "ratio > 1 = покупатели агрессивнее, < 1 = продавцы"
        }
    except Exception as e:
        print(f"[WARN] taker_ratio: {e}"); return None


def fetch_long_short_ratio() -> dict | None:
    """Соотношение лонгов и шортов — глобально и у топ-трейдеров"""
    try:
        # Все трейдеры
        r1 = requests.get(
            f"{BINANCE_F}/futures/data/globalLongShortAccountRatio"
            "?symbol=BTCUSDT&period=1h&limit=4",
            headers=HEADERS, timeout=10
        )
        global_data = r1.json()
        g_current = float(global_data[-1]["longShortRatio"])
        g_prev    = float(global_data[0]["longShortRatio"])

        # Топ-трейдеры (умные деньги)
        r2 = requests.get(
            f"{BINANCE_F}/futures/data/topLongShortPositionRatio"
            "?symbol=BTCUSDT&period=1h&limit=4",
            headers=HEADERS, timeout=10
        )
        top_data = r2.json()
        t_current = float(top_data[-1]["longShortRatio"])
        t_prev    = float(top_data[0]["longShortRatio"])

        # Дивергенция: если толпа лонгует а топ шортит — медвежий сигнал
        divergence = None
        if g_current > 1.2 and t_current < 0.9:
            divergence = "🚨 ДИВЕРГЕНЦИЯ: толпа лонгует, топ-трейдеры шортят — МЕДВЕЖИЙ сигнал"
        elif g_current < 0.8 and t_current > 1.1:
            divergence = "🚨 ДИВЕРГЕНЦИЯ: толпа шортит, топ-трейдеры лонгуют — БЫЧИЙ сигнал"

        return {
            "all_traders_ls":     round(g_current, 3),
            "all_traders_trend":  "лонги растут" if g_current > g_prev else "шорты растут",
            "top_traders_ls":     round(t_current, 3),
            "top_traders_trend":  "топ покупает" if t_current > t_prev else "топ продаёт",
            "divergence_signal":  divergence,
            "note": "Топ-трейдеры — крупные игроки. Их позиции важнее толпы."
        }
    except Exception as e:
        print(f"[WARN] long_short: {e}"); return None


def fetch_deribit_options() -> dict | None:
    """Put/Call ratio и приближение Max Pain с Deribit

    Put/Call > 1 → больше защиты от падения → рынок нервничает
    Max Pain → цена к которой тяготеет рынок перед экспирацией
    """
    try:
        # Получаем все активные BTC опционы
        r = requests.get(
            f"{DERIBIT}/get_book_summary_by_currency?currency=BTC&kind=option",
            headers=HEADERS, timeout=15
        )
        result = r.json().get("result", [])

        if not result:
            return None

        # Считаем Put/Call OI ratio
        call_oi = sum(x.get("open_interest", 0) for x in result if "_C" in x.get("instrument_name", ""))
        put_oi  = sum(x.get("open_interest", 0) for x in result if "_P" in x.get("instrument_name", ""))

        pc_ratio = round(put_oi / call_oi, 3) if call_oi > 0 else 0
        pc_signal = "🔴 страх — больше путов (защита от падения)" if pc_ratio > 1.2 else \
                    "🟢 жадность — больше коллов (ставки на рост)" if pc_ratio < 0.7 else \
                    "⚪ нейтрально"

        # Ближайшая экспирация
        from datetime import datetime
        expiries = {}
        for x in result:
            name = x.get("instrument_name", "")
            parts = name.split("-")
            if len(parts) >= 3:
                exp = parts[1]
                strike = int(parts[2]) if parts[2].isdigit() else 0
                option_type = parts[3] if len(parts) > 3 else ""
                if exp not in expiries:
                    expiries[exp] = {"calls": {}, "puts": {}}
                if strike > 0:
                    oi = x.get("open_interest", 0)
                    if option_type == "C":
                        expiries[exp]["calls"][strike] = expiries[exp]["calls"].get(strike, 0) + oi
                    elif option_type == "P":
                        expiries[exp]["puts"][strike] = expiries[exp]["puts"].get(strike, 0) + oi

        # Находим max pain для ближайшей экспирации
        max_pain = None
        nearest_exp = sorted(expiries.keys())[0] if expiries else None
        if nearest_exp:
            exp_data = expiries[nearest_exp]
            all_strikes = sorted(set(list(exp_data["calls"].keys()) + list(exp_data["puts"].keys())))
            min_pain = float("inf")
            for test_price in all_strikes:
                pain = 0
                for strike, oi in exp_data["calls"].items():
                    pain += max(0, test_price - strike) * oi
                for strike, oi in exp_data["puts"].items():
                    pain += max(0, strike - test_price) * oi
                if pain < min_pain:
                    min_pain = pain
                    max_pain = test_price

        return {
            "put_call_ratio":    pc_ratio,
            "pc_signal":         pc_signal,
            "total_call_oi_btc": round(call_oi, 0),
            "total_put_oi_btc":  round(put_oi, 0),
            "nearest_expiry":    nearest_exp,
            "max_pain_usd":      max_pain,
            "max_pain_note":     f"Рынок тяготеет к ${max_pain:,} перед экспирацией {nearest_exp}" if max_pain else None,
        }
    except Exception as e:
        print(f"[WARN] deribit: {e}"); return None


# ═══════════════════════════════════════════
#  БЛОК 3: НАСТРОЕНИЕ РЫНКА
# ═══════════════════════════════════════════

def fetch_fear_greed() -> dict | None:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=7",
                         headers=HEADERS, timeout=10)
        data = r.json()["data"]
        cur  = data[0]; prev = data[1]
        week_avg = round(sum(int(d["value"]) for d in data) / len(data))
        return {
            "value":     int(cur["value"]),
            "label":     cur["value_classification"],
            "yesterday": int(prev["value"]),
            "week_avg":  week_avg,
            "trend":     "растёт 📈" if int(cur["value"]) > int(prev["value"]) else "падает 📉",
        }
    except Exception as e:
        print(f"[WARN] fear_greed: {e}"); return None


def fetch_global_market() -> dict | None:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global",
                         headers=HEADERS, timeout=10)
        d = r.json()["data"]
        return {
            "btc_dominance":    round(d["market_cap_percentage"]["btc"], 2),
            "total_mcap_change": round(d["market_cap_change_percentage_24h_usd"], 2),
            "total_mcap_T":     round(d["total_market_cap"]["usd"] / 1e12, 3),
        }
    except Exception as e:
        print(f"[WARN] global: {e}"); return None


def fetch_reddit_sentiment() -> dict | None:
    try:
        r = requests.get("https://www.reddit.com/r/Bitcoin/hot.json?limit=15",
                         headers={**HEADERS, "Accept": "application/json"}, timeout=10)
        posts = r.json()["data"]["children"]
        bull_kw = ["bull", "pump", "moon", "ath", "buy", "long", "surge", "rally"]
        bear_kw = ["bear", "dump", "crash", "sell", "short", "drop", "fall"]
        bull = sum(1 for p in posts if any(k in p["data"]["title"].lower() for k in bull_kw))
        bear = sum(1 for p in posts if any(k in p["data"]["title"].lower() for k in bear_kw))
        mood = "бычье" if bull > bear * 1.5 else "медвежье" if bear > bull * 1.5 else "нейтральное"
        return {"bullish_posts": bull, "bearish_posts": bear, "community_mood": mood}
    except Exception as e:
        print(f"[WARN] reddit: {e}"); return None


def fetch_crypto_news() -> list:
    if not CRYPTOPANIC_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_TOKEN}"
            "&currencies=BTC&kind=news&filter=hot",
            headers=HEADERS, timeout=10
        )
        posts = r.json().get("results", [])[:6]
        return [{"title": p["title"],
                 "positive": p.get("votes", {}).get("positive", 0),
                 "negative": p.get("votes", {}).get("negative", 0)} for p in posts]
    except Exception as e:
        print(f"[WARN] news: {e}"); return []


# ═══════════════════════════════════════════
#  АНАЛИЗ ЧЕРЕЗ CLAUDE AI
# ═══════════════════════════════════════════

def analyze_with_claude(data: dict) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    ok = sum(1 for v in data.values() if v)

    prompt = f"""Ты профессиональный криптотрейдер и квант-аналитик.
Перед тобой данные из {ok} источников по Bitcoin — включая опережающие индикаторы движения денег.

━━━ РЫНОЧНЫЕ ДАННЫЕ ━━━━━━━━━━━━━━━━━━━━━━
[ЦЕНА]: {json.dumps(data.get("price"), ensure_ascii=False)}
[ТЕХНИКА SMA/RSI]: {json.dumps(data.get("technical"), ensure_ascii=False)}
[ГЛОБАЛЬНЫЙ РЫНОК]: {json.dumps(data.get("global"), ensure_ascii=False)}

━━━ ДЕРИВАТИВЫ (опережающие сигналы) ━━━━━
[ФАНДИНГ + OI ТРЕНД]: {json.dumps(data.get("funding_oi"), ensure_ascii=False)}
[АГРЕССИВНОСТЬ ТЕЙКЕРОВ]: {json.dumps(data.get("taker"), ensure_ascii=False)}
[LONG/SHORT RATIO]: {json.dumps(data.get("long_short"), ensure_ascii=False)}
[ОПЦИОНЫ DERIBIT (Put/Call, Max Pain)]: {json.dumps(data.get("options"), ensure_ascii=False)}

━━━ НАСТРОЕНИЕ ━━━━━━━━━━━━━━━━━━━━━━━━━━
[FEAR & GREED]: {json.dumps(data.get("fear_greed"), ensure_ascii=False)}
[REDDIT]: {json.dumps(data.get("reddit"), ensure_ascii=False)}
[НОВОСТИ]: {json.dumps(data.get("news"), ensure_ascii=False)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ПРАВИЛА АНАЛИЗА:
1. Деривативы и потоки денег важнее новостей и мнений
2. Дивергенция между топ-трейдерами и толпой — сильный сигнал
3. Экстремальный фандинг + высокий OI = сквиз неизбежен
4. Max Pain — магнит для цены перед экспирацией
5. Тейкер-ратио показывает кто реально агрессивен прямо сейчас

ФОРМАТ ОТВЕТА (строго):

🎯 СИГНАЛ: [БЫЧИЙ / МЕДВЕЖИЙ / НЕЙТРАЛЬНЫЙ]
💪 СИЛА: [1-10] | УВЕРЕННОСТЬ: [низкая/средняя/высокая]

📊 ЧТО ГОВОРЯТ ДЕНЬГИ:
• OI + Фандинг: [вывод]
• Тейкеры: [кто агрессивен сейчас]
• Топ vs Толпа: [есть ли дивергенция]
• Опционы: [Put/Call сигнал + Max Pain]

📉 ЧТО ГОВОРИТ ТЕХНИКА:
• RSI: [вывод]
• SMA: [вывод]
• Позиция в диапазоне: [вывод]

⚡️ КЛЮЧЕВОЙ СИГНАЛ:
[Самое важное из всех данных — одно предложение]

🟢 ДЛЯ ВХОДА В ЛОНГ нужно:
[конкретное условие]

🔴 ДЛЯ ВХОДА В ШОРТ нужно:
[конкретное условие]

💡 СЕЙЧАС:
[Входить / Ждать / Выходить — и почему конкретно]

⛔ СТОП-СИГНАЛ:
[что отменяет этот анализ]

Отвечай только на русском. Без воды. Конкретные данные из таблиц."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1400,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


# ═══════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════

def send_telegram(text: str) -> bool:
    try:
        # Telegram ограничивает 4096 символов
        if len(text) > 4000:
            text = text[:3990] + "\n\n... [обрезано]"
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[ERROR] telegram: {e}"); return False


# ═══════════════════════════════════════════
#  ГЛАВНЫЙ ЦИКЛ
# ═══════════════════════════════════════════

def run_analysis():
    now = datetime.now(timezone.utc)
    print(f"\n{'═'*55}")
    print(f"  🚀 Crypto Signal Bot v2.0")
    print(f"  {now.strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'═'*55}")

    print("📡 Собираю данные...")
    data = {
        "price":      fetch_btc_price(),
        "technical":  fetch_technical_data(),
        "global":     fetch_global_market(),
        "funding_oi": fetch_funding_and_oi(),
        "taker":      fetch_taker_ratio(),
        "long_short": fetch_long_short_ratio(),
        "options":    fetch_deribit_options(),
        "fear_greed": fetch_fear_greed(),
        "reddit":     fetch_reddit_sentiment(),
        "news":       fetch_crypto_news(),
    }

    ok = sum(1 for v in data.values() if v)
    print(f"✅ Источников: {ok}/10")

    if ok < 4:
        print("❌ Слишком мало данных. Пропускаю."); return

    print("🤖 Анализирую через Claude...")
    try:
        analysis = analyze_with_claude(data)
    except Exception as e:
        print(f"[ERROR] Claude: {e}"); return

    # Формируем сообщение
    price = data.get("price") or {}
    fg    = data.get("fear_greed") or {}
    opts  = data.get("options") or {}
    ls    = data.get("long_short") or {}

    price_str  = f"${price.get('price', 0):,.0f}" if price else "N/A"
    change_str = f"{price.get('change_24h', 0):+.1f}%" if price else ""
    fg_str     = f"{fg.get('value', '?')} — {fg.get('label', '?')}" if fg else "N/A"
    mp_str     = f"${opts.get('max_pain_usd', 0):,}" if opts.get("max_pain_usd") else "N/A"
    div_str    = ls.get("divergence_signal", "") or ""

    header = (
        f"🤖 <b>CRYPTO SIGNAL BOT v2.0</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now.strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"₿  BTC: <b>{price_str}</b> ({change_str} / 24ч)\n"
        f"😱 Fear&Greed: <b>{fg_str}</b>\n"
        f"🎯 Max Pain: <b>{mp_str}</b>\n"
    )
    if div_str:
        header += f"{div_str}\n"
    header += (
        f"📡 Источников: {ok}/10\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    message = header + analysis

    print("📨 Отправляю в Telegram...")
    if send_telegram(message):
        print("✅ Отправлено!")
    else:
        print("❌ Ошибка отправки")


if __name__ == "__main__":
    print("🚀 Crypto Signal Bot v2.0 запущен!")
    print(f"⏱  Анализ каждые {ANALYSIS_INTERVAL_H} ч.")
    run_analysis()
    schedule.every(ANALYSIS_INTERVAL_H).hours.do(run_analysis)
    while True:
        schedule.run_pending()
        time.sleep(30)
