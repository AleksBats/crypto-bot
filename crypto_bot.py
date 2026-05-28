"""
╔══════════════════════════════════════════════════════╗
║         CRYPTO SIGNAL BOT  v1.0                      ║
║  Анализирует рынок и отправляет сигналы в Telegram   ║
╚══════════════════════════════════════════════════════╝

Источники данных:
  • CoinGecko       — цена, объём, доминация BTC (бесплатно)
  • Fear & Greed    — индекс страха и жадности (бесплатно)
  • Binance API     — фандинг рейт (бесплатно)
  • CryptoPanic     — новости (бесплатный токен)
  • Reddit          — настроение сообщества (бесплатно)
  • Coinglass       — ликвидации и OI (бесплатно)
  • Claude AI       — финальный анализ и сигнал
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
COINGLASS_API_KEY   = os.getenv("COINGLASS_API_KEY", "")
ANALYSIS_INTERVAL_H = int(os.getenv("ANALYSIS_INTERVAL_HOURS", "6"))

HEADERS = {"User-Agent": "CryptoSignalBot/1.0"}


# ─────────────────────────────────────────
#  ИСТОЧНИК 1: ЦЕНА BTC (CoinGecko)
# ─────────────────────────────────────────
def fetch_btc_price() -> dict | None:
    try:
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd"
            "&include_24hr_change=true"
            "&include_24hr_vol=true"
            "&include_market_cap=true"
            "&include_7d_change=true"
        )
        r = requests.get(url, headers=HEADERS, timeout=10)
        d = r.json()["bitcoin"]
        return {
            "price_usd":       d["usd"],
            "change_24h_pct":  round(d["usd_24h_change"], 2),
            "change_7d_pct":   round(d.get("usd_7d_change", 0), 2),
            "volume_24h_usd":  round(d["usd_24h_vol"] / 1e9, 2),   # в млрд
            "market_cap_usd":  round(d["usd_market_cap"] / 1e9, 1), # в млрд
        }
    except Exception as e:
        print(f"[WARN] fetch_btc_price: {e}")
        return None


# ─────────────────────────────────────────
#  ИСТОЧНИК 2: ГЛОБАЛЬНЫЙ РЫНОК (CoinGecko)
# ─────────────────────────────────────────
def fetch_global_market() -> dict | None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            headers=HEADERS, timeout=10
        )
        d = r.json()["data"]
        return {
            "btc_dominance_pct":           round(d["market_cap_percentage"]["btc"], 2),
            "total_market_cap_change_24h":  round(d["market_cap_change_percentage_24h_usd"], 2),
            "active_cryptocurrencies":      d["active_cryptocurrencies"],
            "total_market_cap_usd_trln":    round(d["total_market_cap"]["usd"] / 1e12, 3),
        }
    except Exception as e:
        print(f"[WARN] fetch_global_market: {e}")
        return None


# ─────────────────────────────────────────
#  ИСТОЧНИК 3: СТРАХ И ЖАДНОСТЬ
# ─────────────────────────────────────────
def fetch_fear_greed() -> dict | None:
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=7",
            headers=HEADERS, timeout=10
        )
        data = r.json()["data"]
        current   = data[0]
        yesterday = data[1]
        week_avg  = round(sum(int(d["value"]) for d in data) / len(data))
        trend = "растёт 📈" if int(current["value"]) > int(yesterday["value"]) else "падает 📉"
        return {
            "value":       int(current["value"]),
            "label":       current["value_classification"],
            "yesterday":   int(yesterday["value"]),
            "week_avg":    week_avg,
            "trend":       trend,
        }
    except Exception as e:
        print(f"[WARN] fetch_fear_greed: {e}")
        return None


# ─────────────────────────────────────────
#  ИСТОЧНИК 4: ФАНДИНГ РЕЙТ (Binance)
# ─────────────────────────────────────────
def fetch_funding_rate() -> dict | None:
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
            headers=HEADERS, timeout=10
        )
        d = r.json()
        funding = float(d["lastFundingRate"]) * 100
        mark_price = float(d["markPrice"])
        index_price = float(d["indexPrice"])
        basis_pct = round((mark_price - index_price) / index_price * 100, 4)
        return {
            "funding_rate_pct":  round(funding, 4),
            "sentiment":         "лонги доминируют" if funding > 0 else "шорты доминируют",
            "mark_price":        round(mark_price, 1),
            "basis_pct":         basis_pct,
        }
    except Exception as e:
        print(f"[WARN] fetch_funding_rate: {e}")
        return None


# ─────────────────────────────────────────
#  ИСТОЧНИК 5: ЛИКВИДАЦИИ (Coinglass)
# ─────────────────────────────────────────
def fetch_liquidations() -> dict | None:
    try:
        if not COINGLASS_API_KEY:
            # Альтернатива без ключа — публичный endpoint
            r = requests.get(
                "https://open-api.coinglass.com/public/v2/liquidation_chart"
                "?symbol=BTC&timeType=h4",
                headers={**HEADERS, "coinglassSecret": ""},
                timeout=10
            )
        else:
            r = requests.get(
                "https://open-api.coinglass.com/public/v2/liquidation_chart"
                "?symbol=BTC&timeType=h4",
                headers={**HEADERS, "coinglassSecret": COINGLASS_API_KEY},
                timeout=10
            )
        data = r.json().get("data", {})
        long_liq  = round(data.get("buyVolUsd", 0) / 1e6, 2)
        short_liq = round(data.get("sellVolUsd", 0) / 1e6, 2)
        dominant  = "лонги" if long_liq > short_liq else "шорты"
        return {
            "long_liquidations_mln":  long_liq,
            "short_liquidations_mln": short_liq,
            "dominant_liq":           dominant,
            "ratio":                  round(long_liq / short_liq, 2) if short_liq > 0 else 0,
        }
    except Exception as e:
        print(f"[WARN] fetch_liquidations: {e}")
        return None


# ─────────────────────────────────────────
#  ИСТОЧНИК 6: НОВОСТИ (CryptoPanic)
# ─────────────────────────────────────────
def fetch_crypto_news() -> list:
    if not CRYPTOPANIC_TOKEN:
        return []
    try:
        url = (
            f"https://cryptopanic.com/api/v1/posts/"
            f"?auth_token={CRYPTOPANIC_TOKEN}"
            f"&currencies=BTC&kind=news&filter=hot"
        )
        r = requests.get(url, headers=HEADERS, timeout=10)
        posts = r.json().get("results", [])[:8]
        news = []
        for p in posts:
            votes = p.get("votes", {})
            news.append({
                "title":    p["title"],
                "source":   p["source"]["title"],
                "positive": votes.get("positive", 0),
                "negative": votes.get("negative", 0),
                "liked":    votes.get("liked", 0),
            })
        return news
    except Exception as e:
        print(f"[WARN] fetch_crypto_news: {e}")
        return []


# ─────────────────────────────────────────
#  ИСТОЧНИК 7: НАСТРОЕНИЕ REDDIT
# ─────────────────────────────────────────
def fetch_reddit_sentiment() -> dict | None:
    try:
        headers = {**HEADERS, "Accept": "application/json"}
        r = requests.get(
            "https://www.reddit.com/r/Bitcoin/hot.json?limit=15",
            headers=headers, timeout=10
        )
        posts = r.json()["data"]["children"]
        total_score = 0
        bullish_keywords = ["bull", "pump", "moon", "ath", "buy", "long", "surge", "rally", "up"]
        bearish_keywords = ["bear", "dump", "crash", "sell", "short", "drop", "fall", "down"]
        bull_count, bear_count = 0, 0

        for post in posts:
            d = post["data"]
            total_score += d["score"]
            title_lower = d["title"].lower()
            if any(k in title_lower for k in bullish_keywords):
                bull_count += 1
            if any(k in title_lower for k in bearish_keywords):
                bear_count += 1

        sentiment = "нейтральное"
        if bull_count > bear_count * 1.5:
            sentiment = "бычье"
        elif bear_count > bull_count * 1.5:
            sentiment = "медвежье"

        return {
            "posts_analyzed":  len(posts),
            "avg_score":       round(total_score / max(len(posts), 1)),
            "bullish_posts":   bull_count,
            "bearish_posts":   bear_count,
            "community_mood":  sentiment,
        }
    except Exception as e:
        print(f"[WARN] fetch_reddit_sentiment: {e}")
        return None


# ─────────────────────────────────────────
#  ИСТОЧНИК 8: ТЕХНИЧЕСКИЕ УРОВНИ (Binance)
# ─────────────────────────────────────────
def fetch_technical_data() -> dict | None:
    try:
        # Последние 50 дневных свечей
        r = requests.get(
            "https://api.binance.com/api/v3/klines"
            "?symbol=BTCUSDT&interval=1d&limit=50",
            headers=HEADERS, timeout=10
        )
        klines = r.json()
        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        vols   = [float(k[5]) for k in klines]

        # SMA
        sma20 = round(sum(closes[-20:]) / 20, 1)
        sma50 = round(sum(closes[-50:]) / 50, 1)
        current = closes[-1]

        # 30-дневный диапазон
        high_30d = round(max(highs[-30:]), 1)
        low_30d  = round(min(lows[-30:]), 1)

        # Объём тренд
        avg_vol_10d = sum(vols[-10:]) / 10
        avg_vol_30d = sum(vols[-30:]) / 30
        vol_trend = "растёт" if avg_vol_10d > avg_vol_30d * 1.1 else "падает" if avg_vol_10d < avg_vol_30d * 0.9 else "стабильный"

        return {
            "sma20":           sma20,
            "sma50":           sma50,
            "price_vs_sma20":  "выше" if current > sma20 else "ниже",
            "price_vs_sma50":  "выше" if current > sma50 else "ниже",
            "high_30d":        high_30d,
            "low_30d":         low_30d,
            "position_in_range_pct": round((current - low_30d) / (high_30d - low_30d) * 100, 1),
            "volume_trend":    vol_trend,
        }
    except Exception as e:
        print(f"[WARN] fetch_technical_data: {e}")
        return None


# ─────────────────────────────────────────
#  АНАЛИЗ ЧЕРЕЗ CLAUDE AI
# ─────────────────────────────────────────
def analyze_with_claude(market_data: dict) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    prompt = f"""Ты профессиональный криптоаналитик. Я собрал данные из {len([v for v in market_data.values() if v])} источников по Bitcoin. Проанализируй их и дай чёткий торговый сигнал.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 ДАННЫЕ ДЛЯ АНАЛИЗА
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[1] ЦЕНА И РЫНОК:
{json.dumps(market_data.get("price"), indent=2, ensure_ascii=False)}

[2] ГЛОБАЛЬНЫЙ РЫНОК:
{json.dumps(market_data.get("global"), indent=2, ensure_ascii=False)}

[3] ИНДЕКС СТРАХА/ЖАДНОСТИ:
{json.dumps(market_data.get("fear_greed"), indent=2, ensure_ascii=False)}

[4] ФАНДИНГ РЕЙТ (Binance):
{json.dumps(market_data.get("funding"), indent=2, ensure_ascii=False)}

[5] ЛИКВИДАЦИИ:
{json.dumps(market_data.get("liquidations"), indent=2, ensure_ascii=False)}

[6] ТЕХНИЧЕСКИЙ АНАЛИЗ (SMA, диапазоны):
{json.dumps(market_data.get("technical"), indent=2, ensure_ascii=False)}

[7] НАСТРОЕНИЕ REDDIT:
{json.dumps(market_data.get("reddit"), indent=2, ensure_ascii=False)}

[8] ГОРЯЧИЕ НОВОСТИ:
{json.dumps(market_data.get("news"), indent=2, ensure_ascii=False)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ТВОЯ ЗАДАЧА — дать анализ строго в этом формате (ничего лишнего):

🎯 СИГНАЛ: [БЫЧИЙ / МЕДВЕЖИЙ / НЕЙТРАЛЬНЫЙ]
💪 СИЛА: [1-10] | УВЕРЕННОСТЬ: [низкая / средняя / высокая]

📊 РАСШИФРОВКА ПО ИСТОЧНИКАМ:
• Цена/техника: [что говорит]
• Фандинг/деривативы: [что говорит]
• Настроение рынка: [что говорит]
• Новости: [что говорит]

⚡️ КЛЮЧЕВОЙ ФАКТОР:
[Самый важный сигнал из всех данных — одним предложением]

🟢 СЦЕНАРИЙ БЫЧИЙ: [при каком условии]
🔴 СЦЕНАРИЙ МЕДВЕЖИЙ: [при каком условии]

💡 РЕКОМЕНДАЦИЯ:
[Конкретно: входить / ждать / выходить, и что должно произойти для входа]

⛔ СТОП-СИГНАЛ:
[Что отменяет этот анализ]

Отвечай только на русском языке. Не делай ценовых прогнозов — оценивай вероятность направления."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


# ─────────────────────────────────────────
#  ОТПРАВКА В TELEGRAM
# ─────────────────────────────────────────
def send_telegram(text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[ERROR] send_telegram: {e}")
        return False


# ─────────────────────────────────────────
#  ГЛАВНЫЙ ЦИКЛ
# ─────────────────────────────────────────
def run_analysis():
    now = datetime.now(timezone.utc)
    print(f"\n{'═'*50}")
    print(f"  Запуск анализа: {now.strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'═'*50}")

    # ── Собираем данные ──
    print("📡 Получаю данные...")
    market_data = {
        "price":        fetch_btc_price(),
        "global":       fetch_global_market(),
        "fear_greed":   fetch_fear_greed(),
        "funding":      fetch_funding_rate(),
        "liquidations": fetch_liquidations(),
        "technical":    fetch_technical_data(),
        "reddit":       fetch_reddit_sentiment(),
        "news":         fetch_crypto_news(),
    }

    sources_ok = sum(1 for v in market_data.values() if v)
    print(f"✅ Получено данных: {sources_ok}/8 источников")

    if sources_ok < 3:
        print("❌ Слишком мало данных для анализа. Пропускаю.")
        return

    # ── Анализируем ──
    print("🤖 Анализирую через Claude AI...")
    try:
        analysis = analyze_with_claude(market_data)
    except Exception as e:
        print(f"[ERROR] Claude API: {e}")
        return

    # ── Формируем сообщение ──
    price = market_data.get("price") or {}
    fg    = market_data.get("fear_greed") or {}

    price_str = f"${price.get('price_usd', 0):,.0f}" if price else "N/A"
    change_str = f"{price.get('change_24h_pct', 0):+.1f}%" if price else ""
    fg_str = f"{fg.get('value', '?')} — {fg.get('label', '?')}" if fg else "N/A"

    message = (
        f"🤖 <b>CRYPTO SIGNAL BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now.strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"₿  BTC: <b>{price_str}</b> <i>({change_str} за 24ч)</i>\n"
        f"😱 Fear&Greed: <b>{fg_str}</b>\n"
        f"📡 Источников: {sources_ok}/8\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{analysis}"
    )

    # ── Отправляем ──
    print("📨 Отправляю в Telegram...")
    if send_telegram(message):
        print("✅ Сообщение отправлено!")
    else:
        print("❌ Ошибка отправки в Telegram")


# ─────────────────────────────────────────
#  ТОЧКА ВХОДА
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Crypto Signal Bot v1.0 запущен!")
    print(f"⏱  Анализ каждые {ANALYSIS_INTERVAL_H} часов")
    print("─" * 40)

    # Запуск сразу при старте
    run_analysis()

    # Планировщик
    schedule.every(ANALYSIS_INTERVAL_H).hours.do(run_analysis)

    while True:
        schedule.run_pending()
        time.sleep(30)
