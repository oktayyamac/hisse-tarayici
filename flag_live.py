#!/usr/bin/env python3
"""
Canlı formasyon alarmı: sert yükseliş sonrası alçalan sıkışma + yukarı kırılım.

Her çalıştırmada TEK tarama yapar (zamanlamayı GitHub Actions yapar):
 1) Gün içinde sert yükselmiş hisseleri bulur (fiyat önceki kapanışa göre >= FLAG_GAIN_MIN %).
 2) Bu hisselerin tamamlanmış 5 dakikalık mumlarında formasyonu arar (flag_backtest.py ile aynı kurallar).
 3) Yeni kırılım varsa Telegram'a alarm gönderir.
 4) Her alarmı kaydeder, seans bitince alarmların sonucunu ve birikmiş istatistiği Telegram'a gönderir.

Gerekli dosyalar aynı klasörde: scanner.py ve flag_backtest.py.
Veri: Alpaca ücretsiz IEX. New York saatiyle 08:00'den önce veri yok.
Ek ayarlar (isteğe bağlı): FLAG_GAIN_MIN (20), FLAG_MAX_AGE (2).
"""
import json
import os
import re
import statistics
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo

import flag_backtest as fb
import scanner as sc

ET = fb.ET
TR = ZoneInfo("Europe/Istanbul")
STATE_FILE = os.path.join(sc.HERE, "flag_state.json")
GAIN_MIN = sc.env_float("FLAG_GAIN_MIN", 20)       # aday: fiyatın önceki kapanışa göre en az % yükselişi
MAX_AGE = int(sc.env_float("FLAG_MAX_AGE", 2))     # kırılım mumu en fazla kaç tamamlanmış mum önce olsun
MAX_CANDS = 150
EXCL = re.compile(r"\b(ETFs?|ETNs?|Funds?|ProShares|iShares|Direxion|VIX|Volatility)\b", re.I)


# ================================================================ durum
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("days", {})
        st.setdefault("history", [])
        return st
    except (OSError, ValueError):
        return {"days": {}, "history": []}


def save_state(st):
    keep = sorted(st["days"])[-10:]
    st["days"] = {d: st["days"][d] for d in keep}
    st["history"] = st["history"][-1000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


# ================================================================ seans durumu
def session_state(clock):
    """'open' (normal seans), 'pre' (seans öncesi, NY 08:00 sonrası), 'closed'."""
    now = sc.parse_ts(clock["timestamp"])
    if clock.get("is_open"):
        return "open", now
    nxt = sc.parse_ts(clock["next_open"])
    if nxt.date() == now.date() and now.hour >= 8:
        return "pre", now
    return "closed", now


# ================================================================ aday bulma
def load_universe():
    url = sc.TRADE_URL + "/v2/assets?" + urllib.parse.urlencode({"status": "active", "asset_class": "us_equity"})
    out = {}
    for a in sc.http_json(url, sc.alpaca_headers()):
        if a.get("tradable") and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX", "ARCA", "BATS") and a["symbol"].isalpha():
            out[a["symbol"]] = a.get("name") or ""
    return out


def find_candidates(avg, names, frac, today):
    pool = [s for s in avg if s in names and avg[s] >= sc.MIN_AVG_VOL and not EXCL.search(names[s])]
    out = []
    for i in range(0, len(pool), 500):
        chunk = pool[i:i + 500]
        url = sc.DATA_URL + "/v2/stocks/snapshots?" + urllib.parse.urlencode({"symbols": ",".join(chunk), "feed": "iex"})
        data = sc.http_json(url, sc.alpaca_headers())
        snaps = data.get("snapshots", data)
        for sym, snap in snaps.items():
            price = (snap.get("latestTrade") or {}).get("p")
            db, pb = snap.get("dailyBar"), snap.get("prevDailyBar")
            if not price:
                continue
            if db and db["t"][:10] == today:             # bugünün günlük mumu var
                prev = (pb or {}).get("c")
                if frac is not None and db["v"] / (avg[sym] * frac) < fb.CAND_RVOL:
                    continue
            else:                                          # seans öncesi: son günlük mum dünün kapanışı
                prev = (db or {}).get("c")
            if not prev:
                continue
            gain = (price / prev - 1) * 100
            if gain >= GAIN_MIN and sc.PRICE_MIN <= price <= sc.PRICE_MAX:
                out.append((gain, sym))
    out.sort(reverse=True)
    return [s for _, s in out[:MAX_CANDS]]


# ================================================================ dedektör (flag_backtest.detect ile aynı kurallar)
def detect_recent(bars, t_min):
    """Sadece t >= t_min olan kırılımlara bakar, en yeni geçerli kırılımı döndürür."""
    n = len(bars)
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    vwap = fb.day_vwaps(bars)
    found = None
    for t in range(max(fb.FLAG_MIN_BARS + 2, t_min), n):
        h = max(range(t), key=lambda i: (highs[i], i))
        flag_len = t - 1 - h
        if flag_len < fb.FLAG_MIN_BARS or flag_len > fb.FLAG_MAX_BARS:
            continue
        H = highs[h]
        L = min(lows[max(0, h - fb.IMPULSE_LOOKBACK):h + 1])
        if L <= 0 or H / L - 1 < fb.IMPULSE_MIN / 100 or H <= L:
            continue
        flag = list(range(h + 1, t))
        F = min(lows[i] for i in flag)
        if (H - F) / (H - L) > fb.RETRACE_MAX:
            continue
        fh = [highs[i] for i in flag]
        s, a = fb.fit_line(fh)
        if s >= 0:
            continue
        r = max(y - (a + s * x) for x, y in enumerate(fh))
        env_t = a + s * (t - (h + 1)) + r
        o, hi, lo, c, v = bars[t]
        if c <= env_t or c <= o:
            continue
        above = sum(1 for i in flag if vwap[i] is not None and bars[i][3] >= vwap[i])
        if above / flag_len < fb.VWAP_FRAC:
            continue
        avg_rng = sum(highs[i] - lows[i] for i in flag) / flag_len
        found = {"t": t, "H": H, "L": L, "F": F, "env": env_t, "flag_len": flag_len,
                 "vwap": vwap[t], "price": c, "strong": avg_rng > 0 and (c - o) >= 2 * avg_rng}
    return found


def check_symbol(raw, cur_slot):
    """Tamamlanmış mumlarda yeni kırılım var mı? Döner: (olay, kırılım dilim no) ya da None."""
    done = [b for b in raw if int(sc.parse_ts(b["t"]).timestamp() // 300) < cur_slot]
    grid, first = fb.build_grid(done)
    if len(grid) < fb.FLAG_MIN_BARS + 3:
        return None
    if sum(b[4] for b in grid) < sc.MIN_DAY_VOL:
        return None
    # kırılım mumu son MAX_AGE+1 tamamlanmış mumdan biri olmalı
    t_min = (cur_slot - 1 - MAX_AGE) - first
    ev = detect_recent(grid, t_min)
    if not ev:
        return None
    return ev, first + ev["t"]


# ================================================================ mesajlar
def format_alert(sym, name, ev, slot):
    start = datetime.fromtimestamp(slot * 300, ET)
    close_et = datetime.fromtimestamp(slot * 300 + 300, ET)
    close_tr = close_et.astimezone(TR)
    risk = (ev["price"] - ev["F"]) / ev["price"] * 100
    kind = "büyük (sıkışma ortalamasının 2 katından fazla)" if ev["strong"] else "normal"
    return (
        f"📐 {sym} formasyon kırılımı" + (f" ({name})" if name else "") + "\n"
        f"Fiyat ${ev['price']:.2f} | önceki tepe ${ev['H']:.2f} | trend çizgisi ${ev['env']:.2f}\n"
        f"Sıkışma dibi ${ev['F']:.2f} (fiyatın %{risk:.0f} altı) | VWAP ${ev['vwap']:.2f}\n"
        f"Sıkışma {ev['flag_len']} mum, kırılım mumu {kind}\n"
        f"Mum kapanışı: {close_et:%H:%M} NY / {close_tr:%H:%M} TR\n"
        f"https://finviz.com/quote.ashx?t={sym}\n"
        f"Not: Geçmiş testte bu formasyonun belirgin bir üstünlüğü bulunmadı. Bakılacak hisse olarak kullan, işlem sinyali değil."
    )


def stat_line(label, vals, cost):
    vals = [v for v in vals if v is not None]
    if not vals:
        return f" {label}: veri yok"
    mean = sum(vals) / len(vals)
    return (f" {label}: n={len(vals)} | ort {mean:+.1f}% (maliyetli {mean - cost:+.1f}%) "
            f"| med {statistics.median(vals):+.1f}% | kazanan %{100 * sum(1 for v in vals if v > 0) / len(vals):.0f}")


STATUS = {"target": "hedefe ulaştı", "stop": "stopa gitti", "eod": "gün sonuna kadar açık"}


def summarize_day(date_str, alerts, history):
    d_ord = date.fromisoformat(date_str).toordinal()
    raw = fb.fetch_intraday(list(alerts), d_ord)
    rows = []
    for sym, a in alerts.items():
        grid, first = fb.build_grid(raw.get(sym, []))
        t = a["slot"] - first
        r = fb.outcomes(grid, {"t": t, "F": a["F"], "H": a["H"]}, "close") if grid and 0 <= t < len(grid) else None
        if not r:
            rows.append(f" {sym}: sonuç hesaplanamadı (veri eksik)")
            continue
        late = fb.outcomes(grid, {"t": t, "F": a["F"], "H": a["H"]}, "late")
        b2 = r["bB"]
        rows.append(
            f" {sym}: giriş ${r['entry']:.2f}, gün sonu {r['eod']:+.1f}%, en yüksek {r['mfe']:+.1f}%, "
            f"en düşük {r['mae']:+.1f}%; stop=sıkışma dibi, hedef=2R: " + (STATUS[b2[1]] if b2 else "hesaplanamadı")
        )
        history.append({"date": date_str, "sym": sym, "eod": r["eod"],
                        "late_eod": late["eod"] if late else None, "bB": b2[1] if b2 else None})
    out = [f"🧾 Formasyon alarmları, gün sonu özeti ({date_str})", f"Alarm sayısı: {len(alerts)}"] + rows
    hist = history
    if hist:
        out += ["", f"Birikmiş sonuç (canlı kayıt, {len(hist)} alarm):",
                stat_line("alarm mumu kapanışından, gün sonu", [h["eod"] for h in hist], fb.COST),
                stat_line("2 mum gecikmeli, gün sonu", [h["late_eod"] for h in hist], fb.COST)]
        b = [h["bB"] for h in hist if h["bB"]]
        if b:
            out.append(f" stop=sıkışma dibi, hedef=2R: hedef %{100 * b.count('target') / len(b):.0f}, "
                       f"stop %{100 * b.count('stop') / len(b):.0f}, gün sonu %{100 * b.count('eod') / len(b):.0f}")
    out += ["", "Not: Birkaç alarmlık sonuç şansa bağlıdır. En az 30-50 alarm birikene kadar sonuca fazla anlam yükleme."]
    return "\n".join(out)


# ================================================================ ana akış
def run():
    sc.check_settings()
    clock = sc.get_clock()
    mode, now = session_state(clock)
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    # biten günlerin özetleri
    for d in sorted(state["days"]):
        day = state["days"][d]
        if day.get("sent") or not day.get("alerts"):
            continue
        if d < today or (d == today and mode == "closed"):
            try:
                sc.send_telegram(summarize_day(d, day["alerts"], state["history"]))
                day["sent"] = True
                sc.log(f"{d} özeti gönderildi.")
            except Exception as e:
                sc.log(f"Özet hatası: {e}")
    if mode == "closed":
        save_state(state)
        sc.log("Piyasa kapalı, çıkılıyor.")
        return

    cur_slot = int(now.timestamp() // 300)
    frac = sc.session_fraction(clock) if mode == "open" else None
    avg = sc.get_avg_volumes(today)
    names = load_universe()
    cands = find_candidates(avg, names, frac, today)
    sc.log(f"{mode}: {len(cands)} aday hisse")

    day = state["days"].setdefault(today, {"alerts": {}, "sent": False})
    raw = fb.fetch_intraday(cands, date.fromisoformat(today).toordinal()) if cands else {}
    new = 0
    for sym in cands:
        if sym in day["alerts"] or sym not in raw:
            continue
        res = check_symbol(raw[sym], cur_slot)
        if not res:
            continue
        ev, slot = res
        sc.send_telegram(format_alert(sym, names.get(sym, ""), ev, slot))
        day["alerts"][sym] = {"slot": slot, "F": ev["F"], "H": ev["H"], "price": ev["price"], "strong": ev["strong"]}
        new += 1
        sc.log(f"ALARM: {sym} ${ev['price']:.2f}")
    save_state(state)
    if not new:
        sc.log("Yeni formasyon yok.")


if __name__ == "__main__":
    run()
