"""
CRYPTO SIGNAL BOT v4.0
- Плановый отчёт каждые 6 часов
- Срочный алерт при обнаружении крупного движения денег (BTC)
- CoinMarketCap: объём по биржам, топ-пары, доминация
- ASTER монитор: объём, киты, фандинг — каждые 15 минут
"""

import os, json, time, schedule, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic

load_dotenv()

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
CLAUDE_API_KEY      = os.getenv("CLAUDE_API_KEY")
CRYPTOPANIC_TOKEN   = os.getenv("CRYPTOPANIC_TOKEN", "")
CMC_API_KEY         = os.getenv("CMC_API_KEY", "")
BSCSCAN_API_KEY     = os.getenv("BSCSCAN_API_KEY", "")
ANALYSIS_INTERVAL_H = int(os.getenv("ANALYSIS_INTERVAL_HOURS", "6"))
ALERT_CHECK_MIN     = int(os.getenv("ALERT_CHECK_MINUTES", "30"))
ASTER_CHECK_MIN     = int(os.getenv("ASTER_CHECK_MINUTES", "15"))

# ASTER контракт на BNB Chain
ASTER_CONTRACT = "0x5dCb2db9b7Cbe5CD77e7Be6c2Dd6B3e0523e2E8"
ASTER_WHALE_THRESHOLD = 50_000  # ASTER — порог "кит"

HEADERS   = {"User-Agent": "CryptoSignalBot/4.0"}
BINANCE_F = "https://fapi.binance.com"
BINANCE_S = "https://api.binance.com"
DERIBIT   = "https://www.deribit.com/api/v2/public"

# Хранит состояние для избежания дублей алертов
last_alert_reasons = set()


# ══════════════════════════════════════════
#  СБОР ДАННЫХ
# ══════════════════════════════════════════

def fetch_btc_price():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
            "&include_24hr_vol=true&include_market_cap=true",
            headers=HEADERS, timeout=10)
        d = r.json().get("bitcoin", {})
        return {
            "price":       d.get("usd", 0),
            "change_24h":  round(d.get("usd_24h_change", 0), 2),
            "volume_24h_B": round(d.get("usd_24h_vol", 0) / 1e9, 2),
            "mcap_B":      round(d.get("usd_market_cap", 0) / 1e9, 1),
        }
    except Exception as e:
        print(f"[WARN] btc_price: {e}"); return None


def fetch_technical_data():
    try:
        r = requests.get(
            f"{BINANCE_S}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=52",
            headers=HEADERS, timeout=10)
        kl = r.json()
        if not isinstance(kl, list) or len(kl) < 22:
            return None
        closes = [float(k[4]) for k in kl]
        highs  = [float(k[2]) for k in kl]
        lows   = [float(k[3]) for k in kl]
        vols   = [float(k[5]) for k in kl]
        cur = closes[-1]
        n = len(closes)
        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-min(50,n):]) / min(50,n)
        vol10 = sum(vols[-10:]) / 10
        vol30 = sum(vols[-min(30,n):]) / min(30,n)
        gains, losses = [], []
        for i in range(-15, -1):
            diff = closes[i+1] - closes[i]
            (gains if diff >= 0 else losses).append(abs(diff))
        avg_g = sum(gains)/len(gains) if gains else 0.001
        avg_l = sum(losses)/len(losses) if losses else 0.001
        rsi = round(100 - 100/(1 + avg_g/avg_l), 1)
        n30 = min(30, n)
        h30, l30 = max(highs[-n30:]), min(lows[-n30:])
        rng = h30 - l30
        return {
            "sma20": round(sma20,0), "sma50": round(sma50,0),
            "price_vs_sma20": "выше" if cur > sma20 else "ниже",
            "price_vs_sma50": "выше" if cur > sma50 else "ниже",
            "rsi14": rsi,
            "rsi_signal": "перекуплен" if rsi>70 else "перепродан" if rsi<30 else "нейтральный",
            "high_30d": round(h30,0), "low_30d": round(l30,0),
            "range_pct": round((cur-l30)/rng*100,1) if rng>0 else 50,
            "volume_trend": "растёт" if vol10>vol30*1.1 else "падает" if vol10<vol30*0.9 else "стабильный",
        }
    except Exception as e:
        print(f"[WARN] technical: {e}"); return None


def fetch_global_market():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global",
                         headers=HEADERS, timeout=10)
        d = r.json().get("data", {})
        return {
            "btc_dominance": round(d.get("market_cap_percentage", {}).get("btc", 0), 2),
            "total_mcap_change": round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
        }
    except Exception as e:
        print(f"[WARN] global: {e}"); return None


def fetch_funding_and_oi():
    try:
        r1 = requests.get(
            f"{BINANCE_F}/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1",
            headers=HEADERS, timeout=10)
        fr = r1.json()
        funding = float(fr[0].get("fundingRate", 0)) * 100 if isinstance(fr, list) and fr else 0.0

        r2 = requests.get(
            f"{BINANCE_F}/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=5",
            headers=HEADERS, timeout=10)
        oi_hist = r2.json()
        if not isinstance(oi_hist, list) or len(oi_hist) < 2:
            return {"funding_rate_pct": round(funding,4),
                    "funding_sentiment": "лонги платят шортам" if funding>0 else "шорты платят лонгам"}

        oi_vals = [float(x.get("sumOpenInterest", 0)) for x in oi_hist]
        oi_now, oi_old = oi_vals[-1], oi_vals[0]
        oi_chg = round((oi_now - oi_old)/oi_old*100, 2) if oi_old > 0 else 0

        signal = "нейтральный"
        if oi_chg > 1 and funding > 0.01:    signal = "бычий тренд усиливается"
        elif oi_chg > 1 and funding < -0.01: signal = "медвежий тренд усиливается"
        elif abs(oi_chg) > 2 and abs(funding) > 0.05: signal = "СКВИЗ ВОЗМОЖЕН"
        elif oi_chg < -1: signal = "делевередж — позиции закрываются"

        return {
            "funding_rate_pct":  round(funding, 4),
            "funding_sentiment": "лонги платят шортам" if funding>0 else "шорты платят лонгам",
            "oi_now_B":          round(oi_now/1e9, 3),
            "oi_change_1h_pct":  oi_chg,
            "oi_signal":         signal,
        }
    except Exception as e:
        print(f"[WARN] funding_oi: {e}"); return None


def fetch_taker_ratio():
    try:
        r = requests.get(
            f"{BINANCE_F}/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=1h&limit=4",
            headers=HEADERS, timeout=10)
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        ratios = [float(x.get("buySellRatio", 1)) for x in data]
        cur, avg = ratios[-1], sum(ratios)/len(ratios)
        return {
            "current_ratio": round(cur, 3),
            "avg_4h": round(avg, 3),
            "interpretation": "покупатели доминируют" if cur>1.1 else
                              "продавцы доминируют" if cur<0.9 else "баланс",
        }
    except Exception as e:
        print(f"[WARN] taker_ratio: {e}"); return None


def fetch_long_short_ratio():
    try:
        r1 = requests.get(
            f"{BINANCE_F}/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=4",
            headers=HEADERS, timeout=10)
        g = r1.json()
        if not isinstance(g, list) or len(g) < 2:
            return None
        g_cur, g_prev = float(g[-1].get("longShortRatio",1)), float(g[0].get("longShortRatio",1))

        r2 = requests.get(
            f"{BINANCE_F}/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=1h&limit=4",
            headers=HEADERS, timeout=10)
        t = r2.json()
        t_cur = float(t[-1].get("longShortRatio",1)) if isinstance(t, list) and len(t) >= 2 else 1.0
        t_prev = float(t[0].get("longShortRatio",1)) if isinstance(t, list) and len(t) >= 2 else 1.0

        divergence = None
        if g_cur > 1.2 and t_cur < 0.9:
            divergence = "ДИВЕРГЕНЦИЯ: толпа лонгует, топ шортит — МЕДВЕЖИЙ сигнал"
        elif g_cur < 0.8 and t_cur > 1.1:
            divergence = "ДИВЕРГЕНЦИЯ: толпа шортит, топ лонгует — БЫЧИЙ сигнал"

        return {
            "all_traders_ls": round(g_cur, 3),
            "all_trend": "лонги растут" if g_cur > g_prev else "шорты растут",
            "top_traders_ls": round(t_cur, 3),
            "top_trend": "топ покупает" if t_cur > t_prev else "топ продаёт",
            "divergence_signal": divergence,
        }
    except Exception as e:
        print(f"[WARN] long_short: {e}"); return None


def fetch_deribit_options():
    try:
        r = requests.get(
            f"{DERIBIT}/get_book_summary_by_currency?currency=BTC&kind=option",
            headers=HEADERS, timeout=15)
        result = r.json().get("result", [])
        if not result:
            return None
        call_oi = sum(float(x.get("open_interest",0)) for x in result if "_C" in str(x.get("instrument_name","")))
        put_oi  = sum(float(x.get("open_interest",0)) for x in result if "_P" in str(x.get("instrument_name","")))
        pc = round(put_oi/call_oi, 3) if call_oi > 0 else 0

        expiries = {}
        for x in result:
            parts = str(x.get("instrument_name","")).split("-")
            if len(parts) < 4 or not parts[2].isdigit(): continue
            exp, strike, opt = parts[1], int(parts[2]), parts[3]
            oi = float(x.get("open_interest",0))
            if exp not in expiries: expiries[exp] = {"calls":{}, "puts":{}}
            bucket = "calls" if opt=="C" else "puts"
            expiries[exp][bucket][strike] = expiries[exp][bucket].get(strike,0) + oi

        max_pain, nearest_exp = None, None
        if expiries:
            nearest_exp = sorted(expiries.keys())[0]
            ed = expiries[nearest_exp]
            strikes = sorted(set(list(ed["calls"].keys()) + list(ed["puts"].keys())))
            min_pain = float("inf")
            for tp in strikes:
                pain = sum(max(0,tp-s)*o for s,o in ed["calls"].items())
                pain += sum(max(0,s-tp)*o for s,o in ed["puts"].items())
                if pain < min_pain: min_pain, max_pain = pain, tp

        return {
            "put_call_ratio": pc,
            "pc_signal": "страх — больше путов" if pc>1.2 else "жадность — больше коллов" if pc<0.7 else "нейтрально",
            "nearest_expiry": nearest_exp,
            "max_pain_usd": max_pain,
        }
    except Exception as e:
        print(f"[WARN] deribit: {e}"); return None


def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=7",
                         headers=HEADERS, timeout=10)
        data = r.json().get("data", [])
        if len(data) < 2: return None
        cur, prev = data[0], data[1]
        return {
            "value": int(cur["value"]),
            "label": cur["value_classification"],
            "yesterday": int(prev["value"]),
            "week_avg": round(sum(int(d["value"]) for d in data)/len(data)),
            "trend": "растёт" if int(cur["value"])>int(prev["value"]) else "падает",
        }
    except Exception as e:
        print(f"[WARN] fear_greed: {e}"); return None


def fetch_reddit_sentiment():
    try:
        r = requests.get("https://www.reddit.com/r/Bitcoin/hot.json?limit=15",
                         headers={**HEADERS, "Accept":"application/json"}, timeout=10)
        posts = r.json()["data"]["children"]
        bull_kw = ["bull","pump","moon","ath","buy","long","surge","rally","up"]
        bear_kw = ["bear","dump","crash","sell","short","drop","fall","down"]
        bull = sum(1 for p in posts if any(k in p["data"]["title"].lower() for k in bull_kw))
        bear = sum(1 for p in posts if any(k in p["data"]["title"].lower() for k in bear_kw))
        return {
            "bullish": bull, "bearish": bear,
            "mood": "бычье" if bull>bear*1.5 else "медвежье" if bear>bull*1.5 else "нейтральное",
        }
    except Exception as e:
        print(f"[WARN] reddit: {e}"); return None


def fetch_coinmarketcap():
    """
    CoinMarketCap — объём по биржам, доминация, активность сети.
    Бесплатный ключ: coinmarketcap.com → Developers → Get API Key
    """
    if not CMC_API_KEY:
        return None
    try:
        headers_cmc = {**HEADERS, "X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}

        # 1. Глобальные метрики (доминация, объём 24h всего рынка)
        r1 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
            headers=headers_cmc, timeout=10)
        gm = r1.json().get("data", {})
        btc_dom   = round(gm.get("btc_dominance", 0), 2)
        total_vol = round(gm.get("quote", {}).get("USD", {}).get("total_volume_24h", 0) / 1e9, 1)
        defi_vol  = round(gm.get("quote", {}).get("USD", {}).get("defi_volume_24h", 0) / 1e9, 2)
        stab_vol  = round(gm.get("quote", {}).get("USD", {}).get("stablecoin_volume_24h", 0) / 1e9, 1)

        # Стабильный объём как % от общего — высокий = рынок уходит в кэш
        stab_pct = round(stab_vol / total_vol * 100, 1) if total_vol > 0 else 0

        # 2. Данные по BTC напрямую
        r2 = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
            "?symbol=BTC&convert=USD",
            headers=headers_cmc, timeout=10)
        btc = r2.json().get("data", {}).get("BTC", {})
        btc_q = btc.get("quote", {}).get("USD", {})
        btc_vol_dom = round(btc_q.get("volume_24h", 0) / (total_vol * 1e9) * 100, 1) if total_vol > 0 else 0
        vol_change  = round(btc_q.get("volume_change_24h", 0), 1)

        # Интерпретация стабильного объёма
        stab_signal = "нейтральный"
        if stab_pct > 35:
            stab_signal = "ВЫСОКИЙ — инвесторы уходят в стейблы, рынок под давлением"
        elif stab_pct < 15:
            stab_signal = "НИЗКИЙ — деньги активно работают в крипте, бычий знак"

        return {
            "btc_dominance_cmc": btc_dom,
            "total_market_vol_24h_B": total_vol,
            "defi_vol_24h_B": defi_vol,
            "stablecoin_vol_24h_B": stab_vol,
            "stablecoin_pct_of_market": stab_pct,
            "stablecoin_signal": stab_signal,
            "btc_vol_dominance_pct": btc_vol_dom,
            "btc_volume_change_24h_pct": vol_change,
        }
    except Exception as e:
        print(f"[WARN] coinmarketcap: {e}"); return None


def fetch_crypto_news():
    if not CRYPTOPANIC_TOKEN: return []
    try:
        r = requests.get(
            f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_TOKEN}"
            "&currencies=BTC&kind=news&filter=hot",
            headers=HEADERS, timeout=10)
        posts = r.json().get("results", [])[:6]
        return [{"title": p["title"],
                 "pos": p.get("votes",{}).get("positive",0),
                 "neg": p.get("votes",{}).get("negative",0)} for p in posts]
    except Exception as e:
        print(f"[WARN] news: {e}"); return []


# ══════════════════════════════════════════
#  СИСТЕМА СРОЧНЫХ АЛЕРТОВ
# ══════════════════════════════════════════

def check_alert_conditions(metrics: dict) -> list[dict]:
    """
    Проверяет условия для срочного алерта.
    Возвращает список сработавших триггеров.
    """
    triggers = []

    fi = metrics.get("funding_oi") or {}
    tk = metrics.get("taker") or {}
    ls = metrics.get("long_short") or {}
    fg = metrics.get("fear_greed") or {}
    pr = metrics.get("price") or {}

    funding = fi.get("funding_rate_pct", 0)
    oi_chg  = fi.get("oi_change_1h_pct", 0)
    taker   = tk.get("current_ratio", 1)
    taker_avg = tk.get("avg_4h", 1)
    change_24h = pr.get("change_24h", 0)

    # 1. ЭКСТРЕМАЛЬНЫЙ ФАНДИНГ — сквиз неизбежен
    if funding > 0.08:
        triggers.append({
            "emoji": "🚨",
            "title": "ЭКСТРЕМАЛЬНЫЙ ФАНДИНГ",
            "detail": f"Фандинг {funding:.4f}% — лонги платят много. Шорт-сквиз или разворот вниз.",
            "key": "high_funding"
        })
    elif funding < -0.08:
        triggers.append({
            "emoji": "🚨",
            "title": "ЭКСТРЕМАЛЬНЫЙ НЕГАТИВНЫЙ ФАНДИНГ",
            "detail": f"Фандинг {funding:.4f}% — шорты платят много. Лонг-сквиз вероятен.",
            "key": "low_funding"
        })

    # 2. РЕЗКИЙ РОСТ OI — крупные деньги заходят
    if oi_chg > 4:
        triggers.append({
            "emoji": "📈",
            "title": "РЕЗКИЙ РОСТ ОТКРЫТОГО ИНТЕРЕСА",
            "detail": f"OI вырос на {oi_chg}% за час. Крупные игроки открывают позиции.",
            "key": "oi_spike_up"
        })
    elif oi_chg < -4:
        triggers.append({
            "emoji": "📉",
            "title": "РЕЗКОЕ ПАДЕНИЕ ОТКРЫТОГО ИНТЕРЕСА",
            "detail": f"OI упал на {abs(oi_chg)}% за час. Массовое закрытие позиций.",
            "key": "oi_spike_down"
        })

    # 3. СКВИЗ OI + ФАНДИНГ вместе
    if abs(oi_chg) > 3 and abs(funding) > 0.05:
        triggers.append({
            "emoji": "💥",
            "title": "СКВИЗ-УСЛОВИЯ АКТИВНЫ",
            "detail": f"OI {oi_chg:+.1f}% + фандинг {funding:.4f}% — рынок перегрет, сквиз близко.",
            "key": "squeeze_conditions"
        })

    # 4. ЭКСТРЕМАЛЬНАЯ АГРЕССИВНОСТЬ ТЕЙКЕРОВ
    if taker > 0 and taker_avg > 0:
        if taker > taker_avg * 1.4 and taker > 1.3:
            triggers.append({
                "emoji": "🟢",
                "title": "ВЗРЫВНОЙ ПОКУПАТЕЛЬСКИЙ СПРОС",
                "detail": f"Тейкер-ратио {taker} vs среднее {taker_avg:.2f}. Покупатели резко доминируют.",
                "key": "taker_buy_spike"
            })
        elif taker < taker_avg * 0.6 and taker < 0.7:
            triggers.append({
                "emoji": "🔴",
                "title": "ВЗРЫВНЫЕ ПРОДАЖИ",
                "detail": f"Тейкер-ратио {taker} vs среднее {taker_avg:.2f}. Продавцы резко доминируют.",
                "key": "taker_sell_spike"
            })

    # 5. ДИВЕРГЕНЦИЯ ТОП vs ТОЛПА
    div = ls.get("divergence_signal")
    if div:
        triggers.append({
            "emoji": "⚠️",
            "title": "ДИВЕРГЕНЦИЯ УМНЫХ ДЕНЕГ",
            "detail": div,
            "key": "divergence"
        })

    # 6. РЕЗКОЕ ИЗМЕНЕНИЕ ЦЕНЫ (за 24ч)
    if change_24h > 8:
        triggers.append({
            "emoji": "🚀",
            "title": "СИЛЬНЫЙ РОСТ BTC",
            "detail": f"BTC вырос на {change_24h:.1f}% за 24ч. Проверь на перегрев.",
            "key": "price_pump"
        })
    elif change_24h < -8:
        triggers.append({
            "emoji": "🩸",
            "title": "СИЛЬНОЕ ПАДЕНИЕ BTC",
            "detail": f"BTC упал на {abs(change_24h):.1f}% за 24ч. Возможна капитуляция.",
            "key": "price_dump"
        })

    # 7. ЭКСТРЕМАЛЬНЫЙ FEAR & GREED
    fg_val = fg.get("value", 50)
    fg_prev = fg.get("yesterday", 50)
    if fg_val >= 85:
        triggers.append({
            "emoji": "🤑",
            "title": "ЭКСТРЕМАЛЬНАЯ ЖАДНОСТЬ",
            "detail": f"Fear&Greed = {fg_val}. Исторически — зона разворота вниз.",
            "key": "extreme_greed"
        })
    elif fg_val <= 15:
        triggers.append({
            "emoji": "😱",
            "title": "ЭКСТРЕМАЛЬНЫЙ СТРАХ",
            "detail": f"Fear&Greed = {fg_val}. Исторически — зона разворота вверх.",
            "key": "extreme_fear"
        })

    # 8. РЕЗКОЕ ИЗМЕНЕНИЕ НАСТРОЕНИЯ
    if abs(fg_val - fg_prev) >= 15:
        direction = "вырос" if fg_val > fg_prev else "упал"
        triggers.append({
            "emoji": "🔄",
            "title": "РЕЗКИЙ СДВИГ НАСТРОЕНИЯ",
            "detail": f"Fear&Greed {direction} на {abs(fg_val-fg_prev)} пунктов за день ({fg_prev} -> {fg_val}).",
            "key": "sentiment_shift"
        })

    return triggers


# ══════════════════════════════════════════
#  ASTER МОНИТОР
# ══════════════════════════════════════════

# Хранит предыдущие значения для сравнения
_aster_prev = {}
_aster_alert_cache = set()


def fetch_aster_price():
    """Цена, объём, изменение ASTER с Binance"""
    try:
        r = requests.get(
            f"{BINANCE_S}/api/v3/ticker/24hr?symbol=ASTERUSDT",
            headers=HEADERS, timeout=10)
        d = r.json()
        if "code" in d:
            return None
        price     = float(d.get("lastPrice", 0))
        change    = float(d.get("priceChangePercent", 0))
        vol_usdt  = float(d.get("quoteVolume", 0))   # объём в USDT за 24ч
        vol_aster = float(d.get("volume", 0))         # объём в ASTER за 24ч
        high      = float(d.get("highPrice", 0))
        low       = float(d.get("lowPrice", 0))
        return {
            "price":       round(price, 4),
            "change_24h":  round(change, 2),
            "vol_usdt_M":  round(vol_usdt / 1e6, 2),   # в миллионах
            "vol_aster_M": round(vol_aster / 1e6, 2),
            "high_24h":    round(high, 4),
            "low_24h":     round(low, 4),
        }
    except Exception as e:
        print(f"[WARN] aster_price: {e}"); return None


def fetch_aster_klines():
    """1-часовые свечи для расчёта среднего объёма"""
    try:
        r = requests.get(
            f"{BINANCE_S}/api/v3/klines?symbol=ASTERUSDT&interval=1h&limit=24",
            headers=HEADERS, timeout=10)
        kl = r.json()
        if not isinstance(kl, list) or len(kl) < 5:
            return None
        vols = [float(k[7]) for k in kl]   # quoteAssetVolume (USDT)
        avg_vol = sum(vols[:-1]) / len(vols[:-1])
        cur_vol = vols[-1]
        spike   = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1
        return {
            "current_hour_vol_usdt": round(cur_vol, 0),
            "avg_hour_vol_usdt":     round(avg_vol, 0),
            "vol_spike_x":           spike,
        }
    except Exception as e:
        print(f"[WARN] aster_klines: {e}"); return None


def fetch_aster_futures():
    """Фандинг и OI по ASTER на Binance Futures"""
    try:
        # Фандинг
        r1 = requests.get(
            f"{BINANCE_F}/fapi/v1/fundingRate?symbol=ASTERUSDT&limit=1",
            headers=HEADERS, timeout=10)
        fr = r1.json()
        funding = float(fr[0].get("fundingRate", 0)) * 100 if isinstance(fr, list) and fr else None

        # OI
        r2 = requests.get(
            f"{BINANCE_F}/fapi/v1/openInterest?symbol=ASTERUSDT",
            headers=HEADERS, timeout=10)
        oi_data = r2.json()
        oi = float(oi_data.get("openInterest", 0)) if "openInterest" in oi_data else None

        # Исторический OI (изменение за час)
        r3 = requests.get(
            f"{BINANCE_F}/futures/data/openInterestHist?symbol=ASTERUSDT&period=1h&limit=3",
            headers=HEADERS, timeout=10)
        oi_hist = r3.json()
        oi_chg = None
        if isinstance(oi_hist, list) and len(oi_hist) >= 2:
            o_new = float(oi_hist[-1].get("sumOpenInterest", 0))
            o_old = float(oi_hist[0].get("sumOpenInterest", 0))
            oi_chg = round((o_new - o_old) / o_old * 100, 2) if o_old > 0 else 0

        if funding is None and oi is None:
            return None
        return {
            "funding_pct":    round(funding, 4) if funding is not None else None,
            "oi_aster":       round(oi, 0) if oi else None,
            "oi_change_1h":   oi_chg,
            "funding_signal": "лонги платят" if (funding or 0) > 0 else "шорты платят",
        }
    except Exception as e:
        print(f"[WARN] aster_futures: {e}"); return None


def fetch_aster_whales():
    """
    Крупные транзакции ASTER через BscScan.
    Бесплатный ключ: bscscan.com/apis
    Без ключа тоже работает но с лимитами.
    """
    try:
        url = (
            f"https://api.bscscan.com/api"
            f"?module=account&action=tokentx"
            f"&contractaddress={ASTER_CONTRACT}"
            f"&page=1&offset=30&sort=desc"
        )
        if BSCSCAN_API_KEY:
            url += f"&apikey={BSCSCAN_API_KEY}"

        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()

        if data.get("status") != "1":
            return None

        txs = data.get("result", [])
        whales = []
        for tx in txs:
            decimals = int(tx.get("tokenDecimal", 18))
            value = int(tx.get("value", 0)) / (10 ** decimals)
            if value >= ASTER_WHALE_THRESHOLD:
                direction = "на биржу" if tx.get("to", "").lower() in [
                    "0x5a52e96bacdabb82fd05763e25335261b270efcb",  # Binance hot wallet
                    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",  # Binance hot 2
                    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance hot 3
                ] else "с биржи" if tx.get("from", "").lower() in [
                    "0x5a52e96bacdabb82fd05763e25335261b270efcb",
                    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
                    "0x28c6c06298d514db089934071355e5743bf21d60",
                ] else "кошелёк→кошелёк"
                whales.append({
                    "amount":    round(value, 0),
                    "direction": direction,
                    "from":      tx.get("from", "")[:10] + "...",
                    "to":        tx.get("to", "")[:10] + "...",
                    "hash":      tx.get("hash", "")[:16] + "...",
                })
        return whales if whales else []
    except Exception as e:
        print(f"[WARN] aster_whales: {e}"); return None


def check_aster_alert_conditions(price_data, kline_data, futures_data, whales):
    """Проверяет условия срочного алерта по ASTER"""
    global _aster_prev, _aster_alert_cache
    triggers = []

    price  = price_data or {}
    klines = kline_data or {}
    fut    = futures_data or {}

    # 1. Всплеск объёма — кто-то крупный заходит
    spike = klines.get("vol_spike_x", 1)
    if spike >= 3:
        key = f"vol_spike_{int(spike)}"
        if key not in _aster_alert_cache:
            triggers.append({
                "emoji": "📊",
                "title": f"ВСПЛЕСК ОБЪЁМА ASTER x{spike}",
                "detail": f"Объём за час ${klines.get('current_hour_vol_usdt',0)/1e3:.0f}K vs среднее ${klines.get('avg_hour_vol_usdt',0)/1e3:.0f}K. Кто-то крупный заходит.",
                "key": key,
            })
    elif spike >= 2:
        key = "vol_spike_2x"
        if key not in _aster_alert_cache:
            triggers.append({
                "emoji": "📈",
                "title": f"РОСТ ОБЪЁМА ASTER x{spike}",
                "detail": f"Объём в 2x выше среднего. Активность растёт.",
                "key": key,
            })

    # 2. Резкое изменение цены за час
    prev_price = _aster_prev.get("price")
    cur_price  = price.get("price", 0)
    if prev_price and cur_price:
        chg_1h = round((cur_price - prev_price) / prev_price * 100, 2)
        if chg_1h > 5:
            key = "aster_pump_1h"
            if key not in _aster_alert_cache:
                triggers.append({
                    "emoji": "🚀",
                    "title": f"ASTER +{chg_1h}% ЗА ЧАС",
                    "detail": f"Цена ${prev_price} → ${cur_price}. Возможно начало разгона.",
                    "key": key,
                })
        elif chg_1h < -5:
            key = "aster_dump_1h"
            if key not in _aster_alert_cache:
                triggers.append({
                    "emoji": "🔴",
                    "title": f"ASTER {chg_1h}% ЗА ЧАС",
                    "detail": f"Цена ${prev_price} → ${cur_price}. Сильное давление продаж.",
                    "key": key,
                })

    # 3. Фандинг экстремальный
    funding = fut.get("funding_pct")
    if funding is not None:
        if funding > 0.05:
            key = "aster_funding_high"
            if key not in _aster_alert_cache:
                triggers.append({
                    "emoji": "💰",
                    "title": f"ВЫСОКИЙ ФАНДИНГ ASTER {funding:.4f}%",
                    "detail": "Лонги сильно перегреты. Возможен шорт-сквиз или разворот вниз.",
                    "key": key,
                })
        elif funding < -0.05:
            key = "aster_funding_low"
            if key not in _aster_alert_cache:
                triggers.append({
                    "emoji": "⚡",
                    "title": f"НЕГАТИВНЫЙ ФАНДИНГ ASTER {funding:.4f}%",
                    "detail": "Шорты перегреты. Лонг-сквиз вероятен.",
                    "key": key,
                })

    # 4. OI резко растёт — крупные игроки открывают позиции
    oi_chg = fut.get("oi_change_1h")
    if oi_chg is not None and oi_chg > 5:
        key = "aster_oi_spike"
        if key not in _aster_alert_cache:
            triggers.append({
                "emoji": "🐋",
                "title": f"OI ASTER РАСТЁТ +{oi_chg}% ЗА ЧАС",
                "detail": "Крупные игроки открывают позиции. Следи за направлением.",
                "key": key,
            })

    # 5. Киты двигают монету
    if whales:
        to_exchange   = [w for w in whales if w["direction"] == "на биржу"]
        from_exchange = [w for w in whales if w["direction"] == "с биржи"]

        if to_exchange:
            total = sum(w["amount"] for w in to_exchange)
            key = f"aster_whale_sell_{int(total/1000)}"
            if key not in _aster_alert_cache:
                triggers.append({
                    "emoji": "⚠️",
                    "title": f"КИТ НЕСЁТ ASTER НА БИРЖУ — {total:,.0f} ASTER",
                    "detail": f"{len(to_exchange)} транзакций на биржу. Возможна продажа. Будь осторожен.",
                    "key": key,
                })

        if from_exchange:
            total = sum(w["amount"] for w in from_exchange)
            key = f"aster_whale_buy_{int(total/1000)}"
            if key not in _aster_alert_cache:
                triggers.append({
                    "emoji": "🟢",
                    "title": f"КИТ ЗАБИРАЕТ ASTER С БИРЖИ — {total:,.0f} ASTER",
                    "detail": f"{len(from_exchange)} транзакций с биржи на кошелёк. Накопление.",
                    "key": key,
                })

    # Обновляем предыдущие значения
    if cur_price:
        _aster_prev["price"] = cur_price

    # Очищаем кэш если накопилось много
    if len(_aster_alert_cache) > 30:
        _aster_alert_cache.clear()

    return triggers


def run_aster_monitor():
    """Запускается каждые 15 минут — мониторинг ASTER"""
    global _aster_alert_cache
    now = datetime.now(timezone.utc)
    print(f"[{now.strftime('%H:%M')}] ASTER монитор...")

    price_data   = fetch_aster_price()
    kline_data   = fetch_aster_klines()
    futures_data = fetch_aster_futures()
    whales       = fetch_aster_whales()

    if not price_data:
        print("  ASTER: нет данных о цене")
        return

    price = price_data.get("price", 0)
    chg   = price_data.get("change_24h", 0)
    vol   = price_data.get("vol_usdt_M", 0)
    print(f"  ASTER: ${price} ({chg:+.1f}%) | Объём 24h: ${vol}M")

    if whales:
        print(f"  Киты: {len(whales)} крупных транзакций")

    triggers = check_aster_alert_conditions(price_data, kline_data, futures_data, whales)

    if not triggers:
        print("  Нет алертов по ASTER.")
        return

    # Формируем сообщение алерта
    spike = (kline_data or {}).get("vol_spike_x", 1)
    funding = (futures_data or {}).get("funding_pct")
    oi_chg  = (futures_data or {}).get("oi_change_1h")

    msg  = f"ASTER ALERT\n{'='*28}\n"
    msg += f"Время: {now.strftime('%d.%m %H:%M')} UTC\n"
    msg += f"Цена: ${price} ({chg:+.1f}% за 24ч)\n"
    msg += f"Объём 24h: ${vol}M\n"
    if spike >= 2:
        msg += f"Объём всплеск: x{spike} от среднего\n"
    if funding is not None:
        msg += f"Фандинг: {funding:.4f}%\n"
    if oi_chg is not None:
        msg += f"OI изменение 1h: {oi_chg:+.2f}%\n"
    msg += f"{'='*28}\n\n"

    msg += "ТРИГГЕРЫ:\n"
    for t in triggers:
        msg += f"{t['emoji']} {t['title']}\n{t['detail']}\n\n"

    if whales:
        msg += f"{'─'*28}\n"
        msg += "КРУПНЫЕ ТРАНЗАКЦИИ:\n"
        for w in whales[:5]:
            msg += f"  {w['direction'].upper()}: {w['amount']:,.0f} ASTER\n"
            msg += f"  {w['from']} → {w['to']}\n\n"

    if send_telegram(msg):
        for t in triggers:
            _aster_alert_cache.add(t["key"])
        print(f"  ASTER алерт отправлен: {[t['key'] for t in triggers]}")
    else:
        print("  Ошибка отправки ASTER алерта")


def send_telegram(text: str) -> bool:
    try:
        safe = text.replace("<","").replace(">","").replace("&","и")
        if len(safe) > 4000:
            safe = safe[:3990] + "\n... [обрезано]"
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": safe},
            timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Telegram {r.status_code}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        print(f"[ERROR] telegram: {e}"); return False


# ══════════════════════════════════════════
#  CLAUDE АНАЛИЗ
# ══════════════════════════════════════════

def analyze_with_claude(data: dict, mode: str = "report") -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    ok = sum(1 for v in data.values() if v)

    if mode == "alert":
        prompt = f"""Ты криптоаналитик. Только что сработали СРОЧНЫЕ алерты по Bitcoin.
Данные: {json.dumps(data, ensure_ascii=False)}

Дай КРАТКИЙ анализ (5-7 строк):
- Что именно происходит с деньгами
- Насколько это серьёзно (1-10)
- Что делать прямо сейчас
- Стоп-уровень

Без воды. Только суть. На русском."""
    else:
        prompt = f"""Ты профессиональный криптотрейдер. Данные из {ok} источников по Bitcoin.

ЦЕНА: {json.dumps(data.get("price"), ensure_ascii=False)}
ТЕХНИКА: {json.dumps(data.get("technical"), ensure_ascii=False)}
ФАНДИНГ+OI: {json.dumps(data.get("funding_oi"), ensure_ascii=False)}
ТЕЙКЕР: {json.dumps(data.get("taker"), ensure_ascii=False)}
LONG/SHORT: {json.dumps(data.get("long_short"), ensure_ascii=False)}
ОПЦИОНЫ: {json.dumps(data.get("options"), ensure_ascii=False)}
FEAR&GREED: {json.dumps(data.get("fear_greed"), ensure_ascii=False)}
CMC (объём рынка, стейблы, доминация): {json.dumps(data.get("coinmarketcap"), ensure_ascii=False)}
REDDIT: {json.dumps(data.get("reddit"), ensure_ascii=False)}
НОВОСТИ: {json.dumps(data.get("news"), ensure_ascii=False)}

ФОРМАТ:

СИГНАЛ: [БЫЧИЙ / МЕДВЕЖИЙ / НЕЙТРАЛЬНЫЙ]
СИЛА: [1-10] | УВЕРЕННОСТЬ: [низкая/средняя/высокая]

ЧТО ГОВОРЯТ ДЕНЬГИ:
- OI + Фандинг: [вывод]
- Тейкеры: [кто агрессивен]
- Топ vs Толпа: [дивергенция?]
- Опционы Max Pain: [вывод]

ТЕХНИКА:
- RSI и SMA: [вывод]

КЛЮЧЕВОЙ СИГНАЛ:
[Самое важное — одно предложение]

ДЛЯ ЛОНГА нужно: [условие]
ДЛЯ ШОРТА нужно: [условие]

СЕЙЧАС: [Входить / Ждать / Выходить]

СТОП-СИГНАЛ: [что отменяет анализ]

Отвечай на русском. Без символов меньше/больше."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000 if mode=="alert" else 1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


# ══════════════════════════════════════════
#  БЫСТРАЯ ПРОВЕРКА АЛЕРТОВ (каждые 30 мин)
# ══════════════════════════════════════════

def run_alert_check():
    global last_alert_reasons
    now = datetime.now(timezone.utc)
    print(f"[{now.strftime('%H:%M')}] Проверка алертов...")

    # Быстро собираем только ключевые метрики
    metrics = {
        "price":        fetch_btc_price(),
        "funding_oi":   fetch_funding_and_oi(),
        "taker":        fetch_taker_ratio(),
        "long_short":   fetch_long_short_ratio(),
        "fear_greed":   fetch_fear_greed(),
        "coinmarketcap": fetch_coinmarketcap(),
    }

    # Дополнительный алерт: стейблкоин-бегство (CMC)
    cmc = metrics.get("coinmarketcap") or {}
    stab_pct = cmc.get("stablecoin_pct_of_market", 0)

    triggers = check_alert_conditions(metrics)

    if stab_pct > 40:
        triggers.append({
            "emoji": "💵",
            "title": "БЕГСТВО В СТЕЙБЛЫ",
            "detail": f"Стейблкоины = {stab_pct}% объёма рынка. Инвесторы массово уходят в кэш — давление на цену.",
            "key": "stablecoin_flight"
        })

    if not triggers:
        print("  Нет сигналов.")
        return

    # Фильтруем уже отправленные (чтобы не спамить)
    new_triggers = [t for t in triggers if t["key"] not in last_alert_reasons]
    if not new_triggers:
        print(f"  {len(triggers)} триггер(ов) уже отправлены ранее.")
        return

    print(f"  АЛЕРТ: {len(new_triggers)} новых триггера!")

    # Получаем краткий анализ от Claude
    try:
        analysis = analyze_with_claude(metrics, mode="alert")
    except Exception as e:
        print(f"[ERROR] Claude alert: {e}")
        analysis = "Анализ недоступен"

    # Формируем сообщение
    price = metrics.get("price") or {}
    price_str = f"${price.get('price',0):,.0f} ({price.get('change_24h',0):+.1f}%)"

    msg = f"СРОЧНЫЙ СИГНАЛ\n"
    msg += f"{'='*28}\n"
    msg += f"BTC: {price_str}\n"
    msg += f"Время: {now.strftime('%d.%m %H:%M')} UTC\n"
    msg += f"{'='*28}\n\n"

    msg += "ТРИГГЕРЫ:\n"
    for t in new_triggers:
        msg += f"{t['emoji']} {t['title']}\n{t['detail']}\n\n"

    msg += f"{'─'*28}\n"
    msg += f"АНАЛИЗ:\n{analysis}"

    if send_telegram(msg):
        # Запоминаем что отправили
        for t in new_triggers:
            last_alert_reasons.add(t["key"])
        print(f"  Алерт отправлен: {[t['key'] for t in new_triggers]}")
    else:
        print("  Ошибка отправки алерта")

    # Сбрасываем кэш через 4 часа (чтобы повторные сигналы снова работали)
    # Делаем это просто — если накопилось много, чистим
    if len(last_alert_reasons) > 20:
        last_alert_reasons = set()


# ══════════════════════════════════════════
#  ПЛАНОВЫЙ ОТЧЁТ (каждые 6 часов)
# ══════════════════════════════════════════

def run_analysis():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*50}")
    print(f"  ПЛАНОВЫЙ ОТЧЁТ | {now.strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'='*50}")

    print("Собираю данные...")
    data = {
        "price":        fetch_btc_price(),
        "technical":    fetch_technical_data(),
        "global":       fetch_global_market(),
        "funding_oi":   fetch_funding_and_oi(),
        "taker":        fetch_taker_ratio(),
        "long_short":   fetch_long_short_ratio(),
        "options":      fetch_deribit_options(),
        "fear_greed":   fetch_fear_greed(),
        "reddit":       fetch_reddit_sentiment(),
        "news":         fetch_crypto_news(),
        "coinmarketcap": fetch_coinmarketcap(),
    }

    ok = sum(1 for v in data.values() if v)
    print(f"Источников: {ok}/11")
    if ok < 3:
        print("Слишком мало данных."); return

    # CMC стейблкоин-сигнал в лог
    cmc = data.get("coinmarketcap") or {}
    if cmc:
        print(f"CMC: стейблы {cmc.get('stablecoin_pct_of_market',0)}% рынка | объём рынка ${cmc.get('total_market_vol_24h_B',0)}B")

    print("Анализирую через Claude...")
    try:
        analysis = analyze_with_claude(data, mode="report")
    except Exception as e:
        print(f"[ERROR] Claude: {e}"); return

    price   = data.get("price") or {}
    fg      = data.get("fear_greed") or {}
    opts    = data.get("options") or {}
    ls      = data.get("long_short") or {}
    cmc     = data.get("coinmarketcap") or {}

    price_str = f"${price.get('price',0):,.0f} ({price.get('change_24h',0):+.1f}%)"
    fg_str    = f"{fg.get('value','?')} — {fg.get('label','?')}"
    mp        = opts.get("max_pain_usd")
    mp_str    = f"${mp:,}" if mp else "N/A"
    div       = ls.get("divergence_signal","") or ""

    header = (
        f"ПЛАНОВЫЙ ОТЧЁТ v3.1\n"
        f"{'='*28}\n"
        f"Время: {now.strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"BTC: {price_str}\n"
        f"Fear&Greed: {fg_str}\n"
        f"Max Pain: {mp_str}\n"
    )
    if cmc:
        stab_pct = cmc.get("stablecoin_pct_of_market", 0)
        mkt_vol  = cmc.get("total_market_vol_24h_B", 0)
        header += f"Объём рынка: ${mkt_vol}B | Стейблы: {stab_pct}%\n"
    if div:
        header += f"ВНИМАНИЕ: {div}\n"
    header += f"Источников: {ok}/11\n{'='*28}\n\n"

    message = header + analysis

    print("Отправляю в Telegram...")
    if send_telegram(message):
        print("Отправлено!")
    else:
        print("Ошибка отправки")


# ══════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════

if __name__ == "__main__":
    print(f"Crypto Signal Bot v4.0 запущен!")
    print(f"Плановый отчёт BTC: каждые {ANALYSIS_INTERVAL_H} ч.")
    print(f"Проверка алертов BTC: каждые {ALERT_CHECK_MIN} мин.")
    print(f"ASTER монитор: каждые {ASTER_CHECK_MIN} мин.")
    print(f"\nBTC алерты:")
    print(f"  - Фандинг > 0.08% или < -0.08%")
    print(f"  - OI изменился > 4% за час")
    print(f"  - Дивергенция топ vs толпа")
    print(f"  - Цена изменилась > 8% за 24ч")
    print(f"  - Fear&Greed > 85 или < 15")
    print(f"\nASTER алерты:")
    print(f"  - Объём в 2x+ выше среднего")
    print(f"  - Цена изменилась > 5% за час")
    print(f"  - Фандинг > 0.05% или < -0.05%")
    print(f"  - OI вырос > 5% за час")
    print(f"  - Кит перевёл > {ASTER_WHALE_THRESHOLD:,} ASTER")

    # Запуск сразу
    run_analysis()
    run_aster_monitor()

    # Планировщик
    schedule.every(ANALYSIS_INTERVAL_H).hours.do(run_analysis)
    schedule.every(ALERT_CHECK_MIN).minutes.do(run_alert_check)
    schedule.every(ASTER_CHECK_MIN).minutes.do(run_aster_monitor)

    while True:
        schedule.run_pending()
        time.sleep(30)
