#!/usr/bin/env python3
"""
Parabolic mover tarayıcı (GitHub Actions sürümü)
ABD hisselerinde olağandışı hacim artışını bulur ve Telegram'a bildirim gönderir.

Her çalıştırmada TEK bir tarama yapar ve çıkar. Zamanlamayı GitHub Actions yapar.
Sadece Python 3.8+ standart kütüphanesini kullanır, ek paket gerekmez.
Veri kaynağı: Alpaca ücretsiz hesap (IEX feed).

Kullanım:
    python scanner.py --once   # tek tarama (piyasa kapalıysa hemen çıkar)
    python scanner.py --test   # Telegram'ı ve veri bağlantısını dener (piyasa kapalıyken de çalışır)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- ayarlar
ALPACA_KEY = os.environ.get("ALPACA_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def env_float(name, default):
    """Boş veya hatalı değerde varsayılana döner."""
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


RVOL_MIN = env_float("RVOL_MIN", 4)          # normalin kaç katı hacim
CHANGE_MIN = env_float("CHANGE_MIN", 5)      # önceki kapanışa göre en az % değişim
PRICE_MIN = env_float("PRICE_MIN", 1)        # en düşük fiyat ($)
PRICE_MAX = env_float("PRICE_MAX", 30)       # en yüksek fiyat ($)
MIN_AVG_VOL = env_float("MIN_AVG_VOL", 2000) # 20 günlük ort. günlük hacim alt sınırı (IEX adedi)
MIN_DAY_VOL = env_float("MIN_DAY_VOL", 5000) # bugünkü hacim alt sınırı (IEX adedi)
PRE_VOL_FRAC = env_float("PRE_VOL_FRAC", 0.1) # seans öncesi: IEX hacmi, ortalama günlük hacmin en az bu oranı olsun
PRE_MIN_VOL = env_float("PRE_MIN_VOL", 1000)  # seans öncesi: en az bu kadar IEX hacmi (seans öncesi işlemler seyrek olduğundan düşük)
MAX_ALERTS_PER_SCAN = 10

TRADE_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
CACHE_FILE = os.path.join(HERE, "avg_cache.json")      # günlük ortalama hacimler
ALERTED_FILE = os.path.join(HERE, "alerted.json")      # bugün bildirilen hisseler


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- http
def http_json(url, headers=None, data=None, retries=4):
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            last = f"HTTP {e.code}: {body}"
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"İstek başarısız ({last}): {url[:90]}")


def alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- telegram
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    http_json(url, data=data)


# ---------------------------------------------------------------- alpaca
def get_clock():
    return http_json(TRADE_URL + "/v2/clock", alpaca_headers())


def session_fraction(clock):
    """Seansın ne kadarının geçtiği (0.1 - 1.0). Piyasa kapalıysa 1."""
    if not clock.get("is_open"):
        return 1.0
    now = parse_ts(clock["timestamp"])
    close = parse_ts(clock["next_close"])
    left = (close - now).total_seconds() / 60
    elapsed = 390 - left
    return min(max(elapsed / 390, 0.1), 1.0)


def get_symbols():
    url = TRADE_URL + "/v2/assets?" + urllib.parse.urlencode(
        {"status": "active", "asset_class": "us_equity"}
    )
    assets = http_json(url, alpaca_headers())
    syms = [
        a["symbol"]
        for a in assets
        if a.get("tradable")
        and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX", "ARCA", "BATS")
        and a["symbol"].isalpha()
    ]
    return sorted(syms)


def fetch_avg_volumes(symbols, today_str):
    """Her hisse için son 20 işlem gününün ortalama günlük hacmi (bugün hariç)."""
    start = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    avg = {}
    total = len(symbols)
    for i in range(0, total, 200):
        chunk = symbols[i:i + 200]
        vols = {}
        token = None
        while True:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": start,
                "limit": 10000,
                "feed": "iex",
                "adjustment": "raw",
            }
            if token:
                params["page_token"] = token
            data = http_json(DATA_URL + "/v2/stocks/bars?" + urllib.parse.urlencode(params), alpaca_headers())
            for sym, bars in (data.get("bars") or {}).items():
                for b in bars:
                    if b["t"][:10] != today_str:
                        vols.setdefault(sym, []).append(b["v"])
            token = data.get("next_page_token")
            if not token:
                break
        for sym, v in vols.items():
            v = v[-20:]
            if len(v) >= 10:
                avg[sym] = sum(v) / len(v)
        log(f"Ortalama hacimler: {min(i + 200, total)}/{total}")
        time.sleep(0.4)
    return avg


def load_cached_avg(today_str):
    """Bugüne ait kayıtlı ortalama hacimler varsa döner, yoksa None."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("date") == today_str and cache.get("avg"):
            return cache["avg"]
    except (OSError, ValueError):
        pass
    return None


def get_avg_volumes(today_str):
    avg = load_cached_avg(today_str)
    if avg is not None:
        return avg
    log("Ortalama hacimler hesaplanıyor (günde bir kez)...")
    avg = fetch_avg_volumes(get_symbols(), today_str)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": today_str, "avg": avg}, f)
    return avg


def load_alerted(today_str):
    try:
        with open(ALERTED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == today_str:
            return set(data.get("syms", []))
    except (OSError, ValueError):
        pass
    return set()


def save_alerted(today_str, syms):
    with open(ALERTED_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": today_str, "syms": sorted(syms)}, f)


# ---------------------------------------------------------------- tarama
def scan_once(symbols, avg, frac, today_str, require_today=True):
    """Döner: (alarmlar, adaylar). Adaylar hacim tabanını geçenlerin tamamı, alarmlar eşikleri de geçenler."""
    candidates = []
    pool = [s for s in symbols if s in avg and avg[s] >= MIN_AVG_VOL]
    for i in range(0, len(pool), 500):
        chunk = pool[i:i + 500]
        url = DATA_URL + "/v2/stocks/snapshots?" + urllib.parse.urlencode(
            {"symbols": ",".join(chunk), "feed": "iex"}
        )
        data = http_json(url, alpaca_headers())
        snaps = data.get("snapshots", data)
        for sym, snap in snaps.items():
            db = snap.get("dailyBar")
            pb = snap.get("prevDailyBar")
            if not db or not pb or not pb.get("c"):
                continue
            if require_today and db["t"][:10] != today_str:
                continue
            vol = db["v"]
            if vol < MIN_DAY_VOL:
                continue
            price = (snap.get("latestTrade") or {}).get("p") or db["c"]
            change = (price / pb["c"] - 1) * 100
            rvol = vol / (avg[sym] * frac)
            candidates.append({"sym": sym, "price": price, "change": change, "rvol": rvol, "vol": vol})

    alerts = [
        c for c in candidates
        if c["rvol"] >= RVOL_MIN
        and c["change"] >= CHANGE_MIN
        and PRICE_MIN <= c["price"] <= PRICE_MAX
    ]
    alerts.sort(key=lambda c: c["rvol"], reverse=True)
    candidates.sort(key=lambda c: c["rvol"], reverse=True)
    return alerts, candidates


def format_alert(c):
    return (
        f"🚀 {c['sym']}  ${c['price']:.2f}  ({c['change']:+.1f}%)\n"
        f"Göreceli hacim: {c['rvol']:.1f}x\n"
        f"Bugünkü hacim (IEX): {int(c['vol']):,}\n"
        f"https://finviz.com/quote.ashx?t={c['sym']}"
    )


# ---------------------------------------------------------------- seans öncesi tarama
def fetch_pre_volumes(syms, today_str):
    """Bugün New York 04:00'dan şimdiye kadar olan IEX hacmi: {sembol: hacim}."""
    y, m, d = map(int, today_str.split("-"))
    start = (datetime(y, m, d, 4, 0, tzinfo=ZoneInfo("America/New_York"))
             .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    vols = {}
    for i in range(0, len(syms), 100):
        token = None
        while True:
            params = {"symbols": ",".join(syms[i:i + 100]), "timeframe": "5Min", "start": start,
                      "limit": 10000, "feed": "iex", "adjustment": "raw"}
            if token:
                params["page_token"] = token
            data = http_json(DATA_URL + "/v2/stocks/bars?" + urllib.parse.urlencode(params), alpaca_headers())
            for sym, bars in (data.get("bars") or {}).items():
                vols[sym] = vols.get(sym, 0) + sum(b["v"] for b in bars)
            token = data.get("next_page_token")
            time.sleep(0.3)
            if not token:
                break
    return vols


def scan_premarket(avg, today_str):
    """Seans öncesi: fiyat önceki kapanışa göre yükselmiş ve seans öncesi hacim ortalama günlük hacme göre yüksek mi."""
    pool = [s for s in avg if avg[s] >= MIN_AVG_VOL]
    cands = []
    for i in range(0, len(pool), 500):
        chunk = pool[i:i + 500]
        url = DATA_URL + "/v2/stocks/snapshots?" + urllib.parse.urlencode({"symbols": ",".join(chunk), "feed": "iex"})
        data = http_json(url, alpaca_headers())
        snaps = data.get("snapshots", data)
        for sym, snap in snaps.items():
            price = (snap.get("latestTrade") or {}).get("p")
            db, pb = snap.get("dailyBar"), snap.get("prevDailyBar")
            if not price:
                continue
            if db and db["t"][:10] == today_str:      # bugünün günlük mumu varsa önceki kapanış prevDailyBar'dır
                prev = (pb or {}).get("c")
            else:                                       # yoksa son günlük mum dünün kapanışıdır
                prev = (db or {}).get("c")
            if not prev:
                continue
            change = (price / prev - 1) * 100
            if change >= CHANGE_MIN and PRICE_MIN <= price <= PRICE_MAX:
                cands.append((change, sym, price))
    cands.sort(reverse=True)
    cands = cands[:200]
    vols = fetch_pre_volumes([s for _, s, _ in cands], today_str) if cands else {}
    alerts = []
    for change, sym, price in cands:
        vol = vols.get(sym, 0)
        if vol >= PRE_MIN_VOL and vol >= PRE_VOL_FRAC * avg[sym]:
            alerts.append({"sym": sym, "price": price, "change": change, "vol": vol,
                           "pct_avg": vol / avg[sym] * 100, "pre": True})
    alerts.sort(key=lambda c: c["pct_avg"], reverse=True)
    return alerts


def format_alert_pre(c):
    return (
        f"🌅 {c['sym']}  ${c['price']:.2f}  ({c['change']:+.1f}%)  [seans öncesi]\n"
        f"Seans öncesi hacim (IEX): {int(c['vol']):,}, normal günlük hacmin %{c['pct_avg']:.0f}'i\n"
        f"https://finviz.com/quote.ashx?t={c['sym']}"
    )


# ---------------------------------------------------------------- ana akış
def need(name, value):
    if not value:
        sys.exit(f"Eksik ayar: {name}. GitHub Secrets bölümünü kontrol et.")


def check_settings():
    need("ALPACA_KEY", ALPACA_KEY)
    need("ALPACA_SECRET", ALPACA_SECRET)
    need("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    need("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)


def run_test():
    check_settings()
    send_telegram("✅ Bot çalışıyor. Test taraması başlıyor...")
    log("Telegram mesajı gönderildi.")

    clock = get_clock()
    today_str = clock["timestamp"][:10]
    avg = get_avg_volumes(today_str)
    log(f"{len(avg)} hissenin ortalama hacmi hazır.")

    # Piyasa kapalı olabilir: son seansın verisiyle, seans oranı 1 alınır.
    alerts, cands = scan_once(list(avg), avg, 1.0, today_str, require_today=False)
    log(f"Eşikleri geçen: {len(alerts)}")
    log("Hacim oranına göre ilk 5 aday (bildirim gönderilmez):")
    for c in cands[:5]:
        log(f"  {c['sym']:6s} ${c['price']:.2f} {c['change']:+.1f}%  {c['rvol']:.1f}x")
    send_telegram(f"✅ Test tamam. {len(avg)} hisse tarandı, eşikleri geçen: {len(alerts)}.")


def run_once():
    check_settings()
    clock = get_clock()
    now = parse_ts(clock["timestamp"])
    if clock.get("is_open"):
        premarket = False
    elif parse_ts(clock["next_open"]).date() == now.date() and now.hour >= 8:
        premarket = True      # piyasa bugün açılacak ve New York 08:00 geçti (IEX verisi 08:00'de başlar)
    else:
        log("Piyasa kapalı, çıkılıyor.")
        return

    today_str = clock["timestamp"][:10]
    avg = get_avg_volumes(today_str)
    alerted = load_alerted(today_str)

    if premarket:
        alerts = scan_premarket(avg, today_str)
    else:
        alerts, _ = scan_once(list(avg), avg, session_fraction(clock), today_str)
    new = [c for c in alerts if c["sym"] not in alerted][:MAX_ALERTS_PER_SCAN]
    for c in new:
        send_telegram(format_alert_pre(c) if c.get("pre") else format_alert(c))
        alerted.add(c["sym"])
        log(f"ALARM{' (seans öncesi)' if c.get('pre') else ''}: {c['sym']} {c['change']:+.1f}%")
    if not new:
        log("Yeni alarm yok.")
    save_alerted(today_str, alerted)


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        run_test()
    else:
        run_once()


if __name__ == "__main__":
    main()
