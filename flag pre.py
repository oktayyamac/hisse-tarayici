#!/usr/bin/env python3
"""
Formasyon alarmı: seans öncesi, normal seans ve seans sonrası (New York 04:00-20:00), Yahoo fiyat mumları, hacimsiz.

Mantık:
 1) Otomatik izleme listesi: son 3 işlem gününde gün içinde sert koşmuş hisseler
    (gün içi yüksek >= önceki kapanışa göre +%RUN_MIN ve hacim >= CAND_RVOL katı). ETF/ETN hariç.
    İsteğe bağlı elle liste: PRE_WATCH (virgülle ayrılmış) listeye eklenir.
    Normal seans ve seans sonrasında listeye ayrıca bugün önceki kapanışa göre sert yükselenler (Alpaca) eklenir.
 2) New York 04:00-20:00 arası her çalışmada listedeki hisselerin 5 dakikalık Yahoo mumlarına bakar ve
    "sert yükseliş sonrası alçalan sıkışma + yukarı kırılım" formasyonunu arar.
    Yahoo'da seans öncesi/sonrası hacim verisi olmadığı için VWAP/hacim şartı YOK, sadece fiyat formasyonu.
 3) Yeni kırılımda Telegram'a alarm (🌅📐 seans öncesi, 📐 normal seans, 🌙📐 seans sonrası),
    alarmları kaydeder, gün sonunda (20:00 NY sonrası) sonuç özeti ve +%10 hedef istatistiği gönderir.

Gerekli dosyalar aynı klasörde: scanner.py, flag_backtest.py, flag_live.py.
Ayarlar (isteğe bağlı): PRE_FLAG_MAX (40 mum), PRE_RETRACE (0.8), PRE_ACTIVE (0.6), PRE_MAX_WATCH (80), PRE_WATCH.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import flag_backtest as fb
import flag_live as fl
import scanner as sc

ET = fb.ET
TR = ZoneInfo("Europe/Istanbul")
STATE_FILE = os.path.join(sc.HERE, "pre_state.json")
WATCH_FILE = os.path.join(sc.HERE, "watch_cache.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

FLAG_MIN = fb.FLAG_MIN_BARS
FLAG_MAX = int(sc.env_float("PRE_FLAG_MAX", 40))      # sıkışma en fazla kaç mum (40 = 3 saat 20 dk)
LOOKBACK = int(sc.env_float("PRE_LOOKBACK", 36))      # yükselişin başlangıcı en fazla kaç mum önce
IMPULSE_MIN = fb.IMPULSE_MIN                          # yükseliş en az %
RETRACE_MAX = sc.env_float("PRE_RETRACE", 0.8)        # geri çekilme / yükseliş
ACTIVE_MIN = sc.env_float("PRE_ACTIVE", 0.6)          # sıkışma mumlarının en az bu oranı gerçek işlemli (high > low) olsun
MAX_AGE = int(sc.env_float("FLAG_MAX_AGE", 2))
MAX_WATCH = int(sc.env_float("PRE_MAX_WATCH", 80))
RUN_DAYS = 3
MANUAL = [s.strip().upper() for s in (os.environ.get("PRE_WATCH") or "").split(",") if s.strip()]


# ================================================================ Yahoo
def yahoo_bars(sym):
    """Döner: (mumlar, hata). Mum: (epoch sn, o, h, l, c, hacim). Seans öncesi hacim Yahoo'da hep 0, kullanılmaz."""
    err = ""
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?" + urllib.parse.urlencode(
            {"interval": "5m", "range": "1d", "includePrePost": "true"})
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            time.sleep(1.5)
            continue
        except Exception as e:
            err = str(e)[:80]
            continue
        chart = data.get("chart") or {}
        res = chart.get("result")
        if not res:
            err = str(chart.get("error"))[:100]
            continue
        r0 = res[0]
        ts = r0.get("timestamp") or []
        q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
        cols = {k: q.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close")}
        bars = []
        for i, t in enumerate(ts):
            c = cols["close"][i]
            if c is None:
                continue
            o, h, l = (cols[k][i] if cols[k][i] is not None else c for k in ("open", "high", "low"))
            bars.append((t, o, max(h, o, c), min(l, o, c), c, 0))
        return bars, None
    return [], err or "bilinmeyen hata"


def build_grid_y(bars, day, cur_slot=None, end_slot=None):
    """Belirli günün mumlarından eksiksiz 5 dk ızgarası. Döner: (mumlar [(o,h,l,c,0)], ilk dilim no)."""
    pts = {}
    for t, o, h, l, c, _ in bars:
        if datetime.fromtimestamp(t, ET).date() != day:
            continue
        s = t // 300
        if (cur_slot is not None and s >= cur_slot) or (end_slot is not None and s >= end_slot):
            continue
        pts[s] = (o, h, l, c, 0)
    if not pts:
        return [], 0
    first, last = min(pts), max(pts)
    grid, prev = [], None
    for s in range(first, last + 1):
        if s in pts:
            grid.append(pts[s])
            prev = pts[s][3]
        else:
            grid.append((prev, prev, prev, prev, 0))
    return grid, first


# ================================================================ dedektör (hacimsiz)
def detect_price(bars, t_min):
    """flag_live.detect_recent ile aynı kurallar, farklar: VWAP şartı yok, sıkışma 40 muma kadar,
    geri çekilme sınırı %80, sıkışma ve kırılım mumları gerçek işlemli olmalı."""
    n = len(bars)
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    found = None
    for t in range(max(FLAG_MIN + 2, t_min), n):
        h = max(range(t), key=lambda i: (highs[i], i))
        flag_len = t - 1 - h
        if flag_len < FLAG_MIN or flag_len > FLAG_MAX:
            continue
        H = highs[h]
        L = min(lows[max(0, h - LOOKBACK):h + 1])
        if L <= 0 or H <= L or H / L - 1 < IMPULSE_MIN / 100:
            continue
        flag = list(range(h + 1, t))
        F = min(lows[i] for i in flag)
        if (H - F) / (H - L) > RETRACE_MAX:
            continue
        fh = [highs[i] for i in flag]
        s, a = fb.fit_line(fh)
        if s >= 0:
            continue
        r = max(y - (a + s * x) for x, y in enumerate(fh))
        env_t = a + s * (t - (h + 1)) + r
        o, hi, lo, c, _ = bars[t]
        if c <= env_t or c <= o or hi <= lo:
            continue
        if sum(1 for i in flag if highs[i] > lows[i]) / flag_len < ACTIVE_MIN:
            continue
        avg_rng = sum(highs[i] - lows[i] for i in flag) / flag_len
        found = {"t": t, "H": H, "L": L, "F": F, "env": env_t, "flag_len": flag_len, "price": c,
                 "strong": avg_rng > 0 and (c - o) >= 2 * avg_rng}
    return found


# ================================================================ durum ve liste
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


def build_watch(avg, names, today_ord):
    """Son RUN_DAYS işlem gününde sert koşmuş hisseler."""
    pool = [s for s in avg if s in names and avg[s] >= sc.MIN_AVG_VOL and not fl.EXCL.search(names[s])]
    start = (datetime.now(timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%d")
    found = []
    for i in range(0, len(pool), 200):
        for sym, bars in fb.fetch_daily(pool[i:i + 200], start).items():
            bars.sort()
            bars = [b for b in bars if b[0] < today_ord]
            n = len(bars)
            if n < 2 or not (sc.PRICE_MIN <= bars[-1][4] <= sc.PRICE_MAX):
                continue
            for k in range(n - 1, max(0, n - 1 - RUN_DAYS), -1):     # en yeni günden geriye
                prev_c, hi, v = bars[k - 1][4], bars[k][2], bars[k][5]
                run = (hi / prev_c - 1) * 100 if prev_c else 0
                if run >= fb.RUN_MIN and v >= fb.CAND_RVOL * avg[sym]:
                    d = date.fromordinal(bars[k][0])
                    found.append((bars[k][0], run, sym,
                                  f"{d:%d.%m} günü +%{run:.0f} koşu, hacim {v / avg[sym]:.0f}x"))
                    break
    found.sort(reverse=True)
    items = [{"sym": s, "reason": r} for _, _, s, r in found[:MAX_WATCH]]
    have = {x["sym"] for x in items}
    return [{"sym": s, "reason": "elle liste"} for s in MANUAL if s not in have] + items


def load_watch(today, avg_fn, names_fn):
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            w = json.load(f)
        if w.get("date") == today:
            return w["items"]
    except (OSError, ValueError):
        pass
    items = build_watch(avg_fn(), names_fn(), date.fromisoformat(today).toordinal())
    with open(WATCH_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": today, "items": items}, f)
    return items


# ================================================================ mesajlar
def session_of(dt):
    m = dt.hour * 60 + dt.minute
    if m < 9 * 60 + 30:
        return "🌅📐", "seans öncesi"
    if m <= 16 * 60:
        return "📐", "normal seans"
    return "🌙📐", "seans sonrası"


def hit_pct(grid, t, F, pct=10):
    """Alarm mumunun kapanışından girişle: önce +%pct mi, önce stop (sıkışma dibi) mu? Aynı mumda ikisi olursa stop sayılır."""
    tgt = grid[t][3] * (1 + pct / 100)
    for b in grid[t + 1:]:
        if b[2] <= F:
            return "stop"
        if b[1] >= tgt:
            return "target"
    return "open"


def format_alert(sym, reason, ev, slot):
    close_et = datetime.fromtimestamp(slot * 300 + 300, ET)
    close_tr = close_et.astimezone(TR)
    icon, sess = session_of(close_et)
    risk = (ev["price"] - ev["F"]) / ev["price"] * 100
    kind = "büyük (sıkışma ortalamasının 2 katından fazla)" if ev["strong"] else "normal"
    return (
        f"{icon} {sym} formasyon kırılımı ({sess})\n"
        f"Listede olma nedeni: {reason}\n"
        f"Fiyat ${ev['price']:.2f} | önceki tepe ${ev['H']:.2f} | trend çizgisi ${ev['env']:.2f}\n"
        f"Sıkışma dibi ${ev['F']:.2f} (fiyatın %{risk:.0f} altı)\n"
        f"Sıkışma {ev['flag_len']} mum ({ev['flag_len'] * 5} dk), kırılım mumu {kind}\n"
        f"Mum kapanışı: {close_et:%H:%M} NY / {close_tr:%H:%M} TR\n"
        f"https://finviz.com/quote.ashx?t={sym}\n"
        f"Not: Yahoo mumlarında hacim yok, bu sadece fiyat formasyonu. Bakılacak hisse olarak kullan, işlem sinyali değil."
    )


def summarize_day(date_str, alerts, history):
    day = date.fromisoformat(date_str)
    end_slot = int(datetime(day.year, day.month, day.day, 20, 0, tzinfo=ET).timestamp() // 300)  # 20:00 NY (seans sonrası bitişi)
    rows = []
    for sym, a in alerts.items():
        bars, err = yahoo_bars(sym)
        time.sleep(0.3)
        grid, first = build_grid_y(bars, day, end_slot=end_slot)
        t = a["slot"] - first
        ev = {"t": t, "F": a["F"], "H": a["H"]}
        r = fb.outcomes(grid, ev, "close") if grid and 0 <= t < len(grid) else None
        if not r:
            rows.append(f" {sym}: sonuç hesaplanamadı (Yahoo verisi alınamadı)")
            continue
        late = fb.outcomes(grid, ev, "late")
        b2 = r["bB"]
        h10 = hit_pct(grid, t, a["F"])
        rows.append(f" {sym}: giriş ${r['entry']:.2f}, 20:00 NY'de {r['eod']:+.1f}%, en yüksek {r['mfe']:+.1f}%, "
                    f"en düşük {r['mae']:+.1f}%; +%10 hedefi: "
                    + {"target": "ulaştı (stoptan önce)", "stop": "ulaşmadan stopa gitti", "open": "ulaşmadı"}[h10])
        history.append({"date": date_str, "sym": sym, "eod": r["eod"],
                        "late_eod": late["eod"] if late else None, "bB": b2[1] if b2 else None, "t10": h10})
    out = [f"🧾 Formasyon alarmları, gün sonu özeti ({date_str})", f"Alarm sayısı: {len(alerts)}"] + rows
    if history:
        out += ["", f"Birikmiş sonuç (canlı kayıt, {len(history)} alarm, 20:00 NY'ye kadar):",
                fl.stat_line("alarm mumu kapanışından", [h["eod"] for h in history], fb.COST),
                fl.stat_line("2 mum gecikmeli", [h["late_eod"] for h in history], fb.COST)]
        t10 = [h["t10"] for h in history if h.get("t10")]
        if t10:
            out.append(f" +%10 hedefi (alarm mumu kapanışından giriş, stop=sıkışma dibi): ulaştı %{100 * t10.count('target') / len(t10):.0f}, "
                       f"stopa gitti %{100 * t10.count('stop') / len(t10):.0f}, ulaşmadı %{100 * t10.count('open') / len(t10):.0f} (n={len(t10)})")
        b = [h["bB"] for h in history if h["bB"]]
        if b:
            out.append(f" stop=sıkışma dibi, hedef=2R: hedef %{100 * b.count('target') / len(b):.0f}, "
                       f"stop %{100 * b.count('stop') / len(b):.0f}, açık %{100 * b.count('eod') / len(b):.0f}")
    out += ["", "Not: Birkaç alarmlık sonuç şansa bağlıdır. En az 30-50 alarm birikene kadar sonuca fazla anlam yükleme."]
    return "\n".join(out)


# ================================================================ ana akış
def run():
    sc.check_settings()
    clock = sc.get_clock()
    now = sc.parse_ts(clock["timestamp"])
    today = now.strftime("%Y-%m-%d")
    if clock.get("is_open"):
        mode = "open"
    elif now.hour < 4 or now.hour >= 20:
        mode = "closed"                   # Yahoo mumları New York 04:00-20:00 arası, gece piyasası için veri yok
    elif sc.parse_ts(clock["next_open"]).date() == now.date():
        mode = "pre"                      # piyasa bugün açılacak
    else:
        mode = "post"                     # seans kapandı (ya da tatil/hafta sonu, o zaman bugünün mumu olmadığından alarm çıkmaz)
    state = load_state()

    for d in sorted(state["days"]):       # biten günlerin özetleri
        day = state["days"][d]
        if day.get("sent") or not day.get("alerts"):
            continue
        if d < today or (d == today and mode == "closed" and now.hour >= 20):   # bugünün özeti ancak 20:00 NY'den sonra
            try:
                sc.send_telegram(summarize_day(d, day["alerts"], state["history"]))
                day["sent"] = True
                sc.log(f"{d} özeti gönderildi.")
            except Exception as e:
                sc.log(f"Özet hatası: {e}")
    if mode == "closed":
        save_state(state)
        sc.log("Tarama penceresi (NY 04:00-20:00) dışında, çıkılıyor.")
        return

    names_cache = {}

    def names_fn():
        names_cache.setdefault("n", fl.load_universe())
        return names_cache["n"]

    watch = load_watch(today, lambda: sc.get_avg_volumes(today), names_fn)
    if mode in ("open", "post"):          # bugün sert yükselenleri de ekle (Alpaca IEX, seans öncesinde seyrek olduğundan orada yok)
        try:
            frac = sc.session_fraction(clock) if mode == "open" else None
            have = {x["sym"] for x in watch}
            extra = [s for s in fl.find_candidates(sc.get_avg_volumes(today), names_fn(), frac, today) if s not in have]
            watch = watch + [{"sym": s, "reason": "bugün yükselenler"} for s in extra[:60]]
        except Exception as e:
            sc.log(f"Bugünün yükselenleri alınamadı: {e}")
    sc.log(f"{mode}: izleme listesi {len(watch)} hisse")
    day_state = state["days"].setdefault(today, {"alerts": {}, "sent": False})
    cur_slot = int(now.timestamp() // 300)
    day_date = now.date()
    errors, new = 0, 0
    for item in watch:
        sym = item["sym"]
        if sym in day_state["alerts"]:
            continue
        bars, err = yahoo_bars(sym)
        time.sleep(0.25)
        if err and not bars:
            errors += 1
            continue
        grid, first = build_grid_y(bars, day_date, cur_slot=cur_slot)
        if len(grid) < FLAG_MIN + 3:
            continue
        ev = detect_price(grid, (cur_slot - 1 - MAX_AGE) - first)
        if not ev:
            continue
        slot = first + ev["t"]
        sc.send_telegram(format_alert(sym, item["reason"], ev, slot))
        day_state["alerts"][sym] = {"slot": slot, "F": ev["F"], "H": ev["H"], "price": ev["price"], "strong": ev["strong"]}
        new += 1
        sc.log(f"ALARM: {sym} ${ev['price']:.2f}")
    if watch and errors == len(watch) and not day_state.get("warned"):
        sc.send_telegram("⚠️ Formasyon botu: Yahoo'dan hiçbir hisse için veri alınamadı. Yahoo GitHub sunucularını engelliyor olabilir.")
        day_state["warned"] = True
    save_state(state)
    if not new:
        sc.log(f"Yeni formasyon yok (Yahoo hatası: {errors}/{len(watch)}).")


if __name__ == "__main__":
    run()
