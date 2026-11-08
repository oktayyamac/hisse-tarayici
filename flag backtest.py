#!/usr/bin/env python3
"""
Formasyon testi: sert yükseliş sonrası alçalan sıkışma (bull flag / daralan üçgen) ve yukarı kırılım.

Adımlar:
 1) Günlük veriyle aday günleri bul (gün içinde sert koşu yapmış, hacimli hisseler).
 2) Bu günlerin 5 dakikalık mumlarını çek (Alpaca IEX; New York saatiyle 08:00'den itibaren).
 3) Mumlarda formasyonu ara, kırılım anından sonra ne olduğunu ölç.
 4) Sonucu Telegram'a gönder.

Aynı klasördeki scanner.py'yi kullanır (anahtarlar ve eşikler ortam değişkenlerinden gelir).
Ek ayarlar (isteğe bağlı): BT_DAYS (varsayılan 120), COST_PCT (varsayılan 1),
RUN_MIN (aday için gün içi koşu %, varsayılan 25), CAND_RVOL (aday hacim katı, varsayılan 3),
FLAG_MIN_BARS (6), FLAG_MAX_BARS (24), IMPULSE_MIN (20), RETRACE_MAX (0.75), VWAP_FRAC (0.6).
"""
import statistics
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import scanner as sc

ET = ZoneInfo("America/New_York")

DAYS = int(sc.env_float("BT_DAYS", 120))
COST = sc.env_float("COST_PCT", 1.0)
RUN_MIN = sc.env_float("RUN_MIN", 25)
CAND_RVOL = sc.env_float("CAND_RVOL", 3)
FLAG_MIN_BARS = int(sc.env_float("FLAG_MIN_BARS", 6))
FLAG_MAX_BARS = int(sc.env_float("FLAG_MAX_BARS", 24))
IMPULSE_MIN = sc.env_float("IMPULSE_MIN", 20)     # % : sıkışmadan önceki yükseliş
IMPULSE_LOOKBACK = 24                              # mum (2 saat)
RETRACE_MAX = sc.env_float("RETRACE_MAX", 0.75)   # geri çekilme / yükseliş
VWAP_FRAC = sc.env_float("VWAP_FRAC", 0.6)        # sıkışma mumlarının VWAP üstünde kapanma oranı
LOOKBACK = 20
CHUNK = 200


# ================================================================ dedektör (ağ kullanmaz)
def fit_line(ys):
    """En küçük kareler doğrusu: y = a + s*x (x = 0, 1, 2, ...)."""
    n = len(ys)
    mx = (n - 1) / 2
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in range(n))
    sxy = sum((x - mx) * (y - my) for x, y in enumerate(ys))
    s = sxy / sxx if sxx else 0.0
    return s, my - s * mx


def day_vwaps(bars):
    """Her mum için günlük kümülatif VWAP (bars: (o, h, l, c, v))."""
    out, pv, vv = [], 0.0, 0.0
    for o, h, l, c, v in bars:
        pv += (h + l + c) / 3 * v
        vv += v
        out.append(pv / vv if vv > 0 else None)
    return out


def detect(bars):
    """İlk geçerli kırılımı döndürür, yoksa None. bars: eksiksiz 5 dakikalık ızgara [(o, h, l, c, v), ...]."""
    n = len(bars)
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    vwap = day_vwaps(bars)
    for t in range(FLAG_MIN_BARS + 2, n):
        h = max(range(t), key=lambda i: (highs[i], i))          # tepe mumu
        flag_len = t - 1 - h
        if flag_len < FLAG_MIN_BARS or flag_len > FLAG_MAX_BARS:
            continue
        H = highs[h]
        L = min(lows[max(0, h - IMPULSE_LOOKBACK):h + 1])       # yükselişin başladığı dip
        if L <= 0 or H / L - 1 < IMPULSE_MIN / 100 or H <= L:
            continue
        flag = list(range(h + 1, t))
        F = min(lows[i] for i in flag)                          # sıkışmanın dibi
        if (H - F) / (H - L) > RETRACE_MAX:
            continue
        fh = [highs[i] for i in flag]
        s, a = fit_line(fh)
        if s >= 0:                                              # tepeler alçalmıyor
            continue
        r = max(y - (a + s * x) for x, y in enumerate(fh))      # çizgiyi tüm tepelerin üstüne taşı
        env_t = a + s * (t - (h + 1)) + r                       # üst trend çizgisinin kırılım mumundaki değeri
        o, hi, lo, c, v = bars[t]
        if c <= env_t or c <= o:                                # yeşil mum, çizginin üstünde kapanmalı
            continue
        above = sum(1 for i in flag if vwap[i] is not None and bars[i][3] >= vwap[i])
        if above / flag_len < VWAP_FRAC:
            continue
        avg_rng = sum(highs[i] - lows[i] for i in flag) / flag_len
        return {
            "t": t, "h": h, "H": H, "L": L, "F": F, "env": env_t, "flag_len": flag_len,
            "strong": avg_rng > 0 and (c - o) >= 2 * avg_rng,   # kırılım mumu, sıkışma ortalamasının 2 katı büyük
        }
    return None


# ================================================================ sonuç ölçümü
def ret(a, b):
    if not a or b is None:
        return None
    return (b / a - 1) * 100


def outcomes(bars, ev, mode):
    """mode 'close': kırılım mumunun kapanışından giriş. 'late': 2 mum sonraki açılıştan giriş (gecikmeli alarm)."""
    n, t = len(bars), ev["t"]
    if mode == "close":
        ei, entry, scan = t, bars[t][3], t + 1
    else:
        ei = t + 2
        if ei >= n:
            return None
        entry, scan = bars[ei][0], ei
    stop = ev["F"]
    if not entry or entry <= stop:
        return None
    risk_pct = (entry - stop) / entry * 100
    last = bars[-1][3]
    tail = bars[scan:]

    def bracket(target):
        for b in tail:
            if b[2] <= stop:                       # aynı mumda ikisi de olursa stop sayılır (temkinli)
                return (ret(entry, stop), "stop", risk_pct)
            if b[1] >= target:
                return (ret(entry, target), "target", risk_pct)
        return (ret(entry, last), "eod", risk_pct)

    return {
        "entry": entry,
        "eod": ret(entry, last),
        "m30": ret(entry, bars[ei + 6][3]) if ei + 6 < n else None,
        "mfe": ret(entry, max(b[1] for b in tail)) if tail else None,
        "mae": ret(entry, min(b[2] for b in tail)) if tail else None,
        "bA": bracket(ev["H"]) if ev["H"] > entry else None,        # hedef: önceki tepe
        "bB": bracket(entry + 2 * (entry - stop)),                  # hedef: 2R
    }


# ================================================================ veri
def to_day_tuple(b):
    return (date.fromisoformat(b["t"][:10]).toordinal(), b["o"], b["h"], b["l"], b["c"], b["v"])


def fetch_daily(chunk, start):
    out, token = {}, None
    while True:
        params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start,
                  "limit": 10000, "feed": "iex", "adjustment": "split"}
        if token:
            params["page_token"] = token
        data = sc.http_json(sc.DATA_URL + "/v2/stocks/bars?" + urllib.parse.urlencode(params), sc.alpaca_headers())
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(to_day_tuple(b) for b in bars)
        token = data.get("next_page_token")
        time.sleep(0.35)
        if not token:
            break
    return out


def find_candidates(bars, cutoff, sym, cands):
    """Gün içinde sert koşu yapmış hacimli günleri bul."""
    n = len(bars)
    if n <= LOOKBACK + 1:
        return
    vols = [b[5] for b in bars]
    s = sum(vols[:LOOKBACK])
    for i in range(LOOKBACK, n):
        avg = s / LOOKBACK
        s += vols[i] - vols[i - LOOKBACK]
        d, o, h, l, c, v = bars[i]
        if d < cutoff:
            continue
        prev = bars[i - 1][4]
        if not prev or avg < sc.MIN_AVG_VOL or v < sc.MIN_DAY_VOL:
            continue
        if not (sc.PRICE_MIN <= prev <= sc.PRICE_MAX):
            continue
        if v / avg >= CAND_RVOL and (h / prev - 1) * 100 >= RUN_MIN:
            cands.append((sym, d))


def fetch_intraday(symbols, d_ord):
    """Bir günün 5 dakikalık mumları (New York 04:00-17:00 arası): {sembol: [mum sözlükleri]}"""
    d = date.fromordinal(d_ord)
    utc = timezone.utc
    start = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET).astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime(d.year, d.month, d.day, 17, 0, tzinfo=ET).astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for i in range(0, len(symbols), 100):
        token = None
        while True:
            params = {"symbols": ",".join(symbols[i:i + 100]), "timeframe": "5Min", "start": start, "end": end,
                      "limit": 10000, "feed": "iex", "adjustment": "split"}
            if token:
                params["page_token"] = token
            data = sc.http_json(sc.DATA_URL + "/v2/stocks/bars?" + urllib.parse.urlencode(params), sc.alpaca_headers())
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = data.get("next_page_token")
            time.sleep(0.35)
            if not token:
                break
    return out


def build_grid(raw):
    """Eksik 5 dakikalık dilimleri bir önceki kapanışla doldurup eksiksiz ızgara üretir. Döner: (mumlar, ilk dilim no)."""
    pts = {}
    for b in raw:
        pts[int(sc.parse_ts(b["t"]).timestamp() // 300)] = (b["o"], b["h"], b["l"], b["c"], b["v"])
    if not pts:
        return [], 0
    first, last = min(pts), max(pts)
    grid, prev_c = [], None
    for slot in range(first, last + 1):
        if slot in pts:
            grid.append(pts[slot])
            prev_c = pts[slot][3]
        else:
            grid.append((prev_c, prev_c, prev_c, prev_c, 0))
    return grid, first


# ================================================================ rapor
def summarize(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return len(vals), sum(vals) / len(vals), statistics.median(vals), 100 * sum(1 for v in vals if v > 0) / len(vals)


def line(label, vals):
    s = summarize(vals)
    if not s:
        return f" {label}: veri yok"
    n, mean, med, win = s
    return f" {label}: n={n} | ort {mean:+.1f}% (maliyetli {mean - COST:+.1f}%) | med {med:+.1f}% | kazanan %{win:.0f}"


def blines(label, items):
    items = [x for x in items if x and x[0] is not None]
    if not items:
        return f" {label}: veri yok"
    n = len(items)
    tg = 100 * sum(1 for x in items if x[1] == "target") / n
    st = 100 * sum(1 for x in items if x[1] == "stop") / n
    mean = sum(x[0] for x in items) / n
    mean_r = sum(x[0] / x[2] for x in items) / n
    return (f" {label}: n={n} | hedef %{tg:.0f} stop %{st:.0f} gün sonu %{100 - tg - st:.0f} "
            f"| ort {mean:+.1f}% (maliyetli {mean - COST:+.1f}%) | {mean_r:+.2f}R")


def med_of(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def section(title, res):
    if not res:
        return [title + " veri yok"]
    out = [f"{title} (n={len(res)})"]
    out.append(line("gün sonu kapanışta sat", [r["eod"] for r in res]))
    out.append(line("30 dk sonra sat", [r["m30"] for r in res]))
    out.append(blines("stop=sıkışma dibi, hedef=önceki tepe", [r["bA"] for r in res]))
    out.append(blines("stop=sıkışma dibi, hedef=2R", [r["bB"] for r in res]))
    mfe, mae = med_of([r["mfe"] for r in res]), med_of([r["mae"] for r in res])
    if mfe is not None and mae is not None:
        out.append(f" tipik en yüksek yükseliş {mfe:+.1f}%, en derin düşüş {mae:+.1f}% (medyan)")
    return out


def build_report(events, n_cands):
    out = [f"📈 Formasyon testi (alçalan sıkışma + yukarı kırılım), son {DAYS} gün",
           f"Aday gün (gün içi ≥+%{RUN_MIN:g} koşu, hacim ≥{CAND_RVOL:g}x): {n_cands}",
           f"Formasyon bulunan: {len(events)}", ""]
    for mode, name in (("close", "Kırılım mumunun kapanışından alsaydın"),
                       ("late", "2 mum geç (gecikmeli alarm) alsaydın")):
        res_all = [e[mode] for e in events if e[mode]]
        res_strong = [e[mode] for e in events if e[mode] and e["strong"]]
        out.append(f"{name}:")
        out += section(" Tümü", res_all)
        out += section(" Sadece güçlü kırılım (mum, sıkışma ortalamasının 2 katı)", res_strong)
        out.append("")
    recent = sorted(events, key=lambda e: e["day"], reverse=True)[:8]
    if recent:
        out.append("Son bulunan örnekler (grafikte kontrol edebilirsin, saat New York):")
        for e in recent:
            r = e["close"]
            tail = f"giriş ${r['entry']:.2f}, gün sonu {r['eod']:+.1f}%" if r else ""
            out.append(f" {e['sym']} {date.fromordinal(e['day']).isoformat()} {e['clock']} {tail}")
        out.append("")
    out += ["Dikkat:",
            "- Veri IEX kaynaklı ve New York 08:00'den başlıyor. Daha erken seans öncesi mumlar yok, seyrek işlem gören hisselerde mum eksik olabilir.",
            "- Delist olan hisseler yok, sonuçlar gerçekte olduğundan iyi görünebilir.",
            f"- Maliyetli sütun %{COST:g} gidiş-dönüş maliyet düşer. Küçük hisselerde gerçek maliyet ve kayma daha yüksektir.",
            "- Örnek sayısı azsa sonuç şansa bağlı olabilir. Kuralları bu sonuca göre fazla ince ayarlamak yanıltır.",
            "- Geçmiş sonuç gelecek garantisi değildir, yatırım tavsiyesi değildir."]
    return "\n".join(out)


def send_long(text, limit=3800):
    part = ""
    for ln in text.split("\n"):
        if len(part) + len(ln) + 1 > limit:
            sc.send_telegram(part)
            part = ""
        part += ln + "\n"
    if part.strip():
        sc.send_telegram(part)


# ================================================================ ana akış
def main():
    sc.check_settings()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=DAYS)).date().toordinal()
    start = (now - timedelta(days=DAYS + 45)).strftime("%Y-%m-%d")

    sc.send_telegram(f"⏳ Formasyon testi başladı (son {DAYS} gün). 10-20 dakika sürebilir.")
    symbols = sc.get_symbols()
    sc.log(f"{len(symbols)} hisse bulundu. Aday günler aranıyor...")

    cands = []
    for i in range(0, len(symbols), CHUNK):
        for sym, bars in fetch_daily(symbols[i:i + CHUNK], start).items():
            bars.sort()
            find_candidates(bars, cutoff, sym, cands)
        sc.log(f"{min(i + CHUNK, len(symbols))}/{len(symbols)} hisse tarandı, aday gün: {len(cands)}")

    by_day = {}
    for sym, d in cands:
        by_day.setdefault(d, []).append(sym)

    events = []
    for k, d in enumerate(sorted(by_day), 1):
        raw = fetch_intraday(by_day[d], d)
        for sym, bars in raw.items():
            grid, first = build_grid(bars)
            if len(grid) < FLAG_MIN_BARS + 3:
                continue
            ev = detect(grid)
            if not ev:
                continue
            clock = datetime.fromtimestamp((first + ev["t"]) * 300, ET).strftime("%H:%M")
            events.append({"sym": sym, "day": d, "clock": clock, "strong": ev["strong"],
                           "close": outcomes(grid, ev, "close"), "late": outcomes(grid, ev, "late")})
        sc.log(f"gün {k}/{len(by_day)} işlendi, formasyon: {len(events)}")

    report = build_report(events, len(cands))
    print(report)
    send_long(report)


if __name__ == "__main__":
    main()
