#!/usr/bin/env python3
"""
Geçmiş veri testi (backtest)

Alpaca'nın günlük geçmiş verisiyle, botun alarm verecek olduğu günleri bulur
(hacim normalin X katı, fiyat +Y% yükselmiş, fiyat aralığı uygun) ve alarmdan
sonraki günlerde ne olduğunu ölçer. Sonucu Telegram'a gönderir.

Aynı klasördeki scanner.py'yi kullanır (anahtarlar ve eşikler aynı ortam değişkenlerinden gelir).
Ek ayarlar (isteğe bağlı): BT_DAYS (kaç gün geriye, varsayılan 365), COST_PCT (gidiş-dönüş maliyet %, varsayılan 1).
"""
import statistics
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import scanner as sc

DAYS = int(sc.env_float("BT_DAYS", 365))
COST = sc.env_float("COST_PCT", 1.0)
LOOKBACK = 20
RVOL_GRID = (3, 4, 6, 10)
CHG_GRID = (5, 10, 20)
CHUNK = 200


# ---------------------------------------------------------------- veri
def fetch_chunk(chunk, start):
    """Bir hisse grubunun günlük çubukları: {sembol: [(gün_no, o, h, l, c, v), ...]}"""
    out = {}
    token = None
    while True:
        params = {
            "symbols": ",".join(chunk),
            "timeframe": "1Day",
            "start": start,
            "limit": 10000,
            "feed": "iex",
            "adjustment": "split",  # bölünmeleri düzelt, yoksa sahte sıçramalar olay sayılır
        }
        if token:
            params["page_token"] = token
        data = sc.http_json(
            sc.DATA_URL + "/v2/stocks/bars?" + urllib.parse.urlencode(params), sc.alpaca_headers()
        )
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(
                (date.fromisoformat(b["t"][:10]).toordinal(), b["o"], b["h"], b["l"], b["c"], b["v"])
                for b in bars
            )
        token = data.get("next_page_token")
        time.sleep(0.35)
        if not token:
            break
    return out


def ret(a, b):
    """a'dan b'ye yüzde getiri."""
    if not a or b is None:
        return None
    return (b / a - 1) * 100


def process(bars, cutoff, base, events, sym):
    n = len(bars)
    if n <= LOOKBACK + 1:
        return
    vols = [b[5] for b in bars]
    s = sum(vols[:LOOKBACK])
    for i in range(LOOKBACK, n):
        avg = s / LOOKBACK                      # önceki 20 günün ortalama hacmi
        s += vols[i] - vols[i - LOOKBACK]       # pencereyi bir sonraki gün için kaydır
        d, o, h, l, c, v = bars[i]
        if d < cutoff:
            continue
        prev = bars[i - 1][4]
        if not prev or avg < sc.MIN_AVG_VOL or v < sc.MIN_DAY_VOL:
            continue
        if not (sc.PRICE_MIN <= c <= sc.PRICE_MAX):
            continue

        def nxt(k, maxgap):
            j = i + k
            if j < n and bars[j][0] - d <= maxgap:
                return bars[j]
            return None

        n1, n3, n5 = nxt(1, 4), nxt(3, 8), nxt(5, 12)

        # kıyas: uygun tüm hisse-günleri
        if n1:
            base["cc"][0] += ret(c, n1[4])
            base["cc"][1] += 1
            base["oc"][0] += ret(n1[1], n1[4])
            base["oc"][1] += 1

        chg = (c / prev - 1) * 100
        rvol = v / avg
        if rvol >= min(RVOL_GRID) and chg >= min(CHG_GRID):
            events.append({
                "sym": sym,
                "rvol": rvol,
                "chg": chg,
                "gap": ret(c, n1[1]) if n1 else None,
                "cc1": ret(c, n1[4]) if n1 else None,
                "cc3": ret(c, n3[4]) if n3 else None,
                "cc5": ret(c, n5[4]) if n5 else None,
                "oc1": ret(n1[1], n1[4]) if n1 else None,
                "oc3": ret(n1[1], n3[4]) if (n1 and n3) else None,
                "oc5": ret(n1[1], n5[4]) if (n1 and n5) else None,
            })


# ---------------------------------------------------------------- rapor
def summarize(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return (
        len(vals),
        sum(vals) / len(vals),
        statistics.median(vals),
        100 * sum(1 for v in vals if v > 0) / len(vals),
    )


def line(label, vals):
    s = summarize(vals)
    if not s:
        return f" {label}: veri yok"
    n, mean, med, win = s
    return (
        f" {label}: n={n} | ort {mean:+.1f}% (maliyetli {mean - COST:+.1f}%) "
        f"| med {med:+.1f}% | kazanan %{win:.0f}"
    )


def grid(events, key):
    rows = []
    for r in RVOL_GRID:
        for c in CHG_GRID:
            s = summarize([e[key] for e in events if e["rvol"] >= r and e["chg"] >= c])
            if s:
                n, mean, med, win = s
                rows.append(f" {r:>2}x/+{c:>2}%: n={n:<5} ort {mean:+.1f}% med {med:+.1f}% kaz %{win:.0f}")
            else:
                rows.append(f" {r:>2}x/+{c:>2}%: veri yok")
    return "\n".join(rows)


def build_report(events, base, n_symbols):
    sel = [e for e in events if e["rvol"] >= sc.RVOL_MIN and e["chg"] >= sc.CHANGE_MIN]
    out = []
    out.append(f"📊 Geçmiş veri testi: son {DAYS} gün, {n_symbols} hisse")
    out.append(
        f"Kural: hacim ≥{sc.RVOL_MIN:g}x, değişim ≥+{sc.CHANGE_MIN:g}%, "
        f"fiyat {sc.PRICE_MIN:g}-{sc.PRICE_MAX:g}$"
    )
    out.append(f"Olay sayısı: {len(sel)}")
    out.append("")
    out.append("A) Alarm günü kapanışından alsaydın:")
    out.append(line("ertesi gün açılışta sat", [e["gap"] for e in sel]))
    out.append(line("ertesi gün kapanışta sat", [e["cc1"] for e in sel]))
    out.append(line("3 gün sonra sat", [e["cc3"] for e in sel]))
    out.append(line("5 gün sonra sat", [e["cc5"] for e in sel]))
    out.append("")
    out.append("B) Ertesi gün açılıştan alsaydın:")
    out.append(line("aynı gün kapanışta sat", [e["oc1"] for e in sel]))
    out.append(line("3 gün sonra sat", [e["oc3"] for e in sel]))
    out.append(line("5 gün sonra sat", [e["oc5"] for e in sel]))
    out.append("")
    if base["cc"][1]:
        out.append(
            f"Kıyas (uygun tüm hisse-günleri): ertesi kapanış ort "
            f"{base['cc'][0] / base['cc'][1]:+.2f}%, ertesi açılıştan alınan "
            f"{base['oc'][0] / base['oc'][1]:+.2f}% (n={base['cc'][1]})"
        )
        out.append("")
    out.append("Eşik tablosu, A (kapanıştan al, ertesi kapanışta sat):")
    out.append(grid(events, "cc1"))
    out.append("")
    out.append("Eşik tablosu, B (ertesi açılıştan al, aynı gün kapanışta sat):")
    out.append(grid(events, "oc1"))
    out.append("")
    out.append("Dikkat:")
    out.append("- Günlük veri: alarmın gün içinde tam ne zaman geleceği bilinmiyor.")
    out.append("- Borsadan çıkan (delist) hisseler listede yok, sonuçlar gerçekte olduğundan iyi görünebilir.")
    out.append(f"- Maliyetli sütun %{COST:g} gidiş-dönüş maliyet düşer. Küçük hisselerde gerçek maliyet daha yüksek olabilir.")
    out.append("- Ortalama birkaç uç değerden etkilenir, medyana da bak. Geçmiş sonuç gelecek garantisi değildir.")
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


# ---------------------------------------------------------------- ana akış
def main():
    sc.check_settings()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=DAYS)).date().toordinal()
    start = (now - timedelta(days=DAYS + 45)).strftime("%Y-%m-%d")

    sc.send_telegram(f"⏳ Geçmiş veri testi başladı (son {DAYS} gün). Birkaç dakika sürer.")
    symbols = sc.get_symbols()
    sc.log(f"{len(symbols)} hisse bulundu.")

    events = []
    base = {"cc": [0.0, 0], "oc": [0.0, 0]}
    with_bars = 0
    for i in range(0, len(symbols), CHUNK):
        data = fetch_chunk(symbols[i:i + CHUNK], start)
        for sym, bars in data.items():
            bars.sort()
            with_bars += 1
            process(bars, cutoff, base, events, sym)
        sc.log(f"{min(i + CHUNK, len(symbols))}/{len(symbols)} hisse işlendi, aday olay: {len(events)}")

    report = build_report(events, base, with_bars)
    print(report)
    send_long(report)


if __name__ == "__main__":
    main()
