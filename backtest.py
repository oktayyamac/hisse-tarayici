#!/usr/bin/env python3
"""
Düşen trend kırılımı sinyallerinin GEÇMİŞ başarısını ölçer (15 dakikalık mum, Yahoo'nun verdiği son ~60 gün).

scanner.py ile AYNI kırılım mantığını kullanır (scanner.detect_breakout), her mumda geriye dönük yürütür ve
her sinyalin sonucunu simüle eder:

  * Giriş gerçekçi: sinyal mumu kapanır, bot haber verir, sen bir sonraki mumun açılışında girersin
    (hassasiyet için "bir sonraki mumun kapanışında gir" de hesaplanır).
  * SL ve TP1/TP2/TP3 seviyeleri sinyaldeki gibi sabit. Aynı mumda hem SL hem TP görülürse SL ÖNCE sayılır.
  * Pozisyon, girişin yapıldığı seansın sonunda (son mumun kapanışı) kapatılır.
  * Maliyet (spread + kayma) COST_PCT ile düşülür (varsayılan %0.3, tur başına).
  * KIYAS: botun hisse filtrelerini (hareketlilik, likidite) geçen hisselerde rastgele mumlardan, aynı SL/TP
    mantığıyla girilseydi ne olurdu. Sinyalin bir
    üstünlüğü olup olmadığını ancak bu kıyas söyler (SL=1R, TP1=1R'de rastgele giriş bile ~%45-50 TP1 görür).
  * Mevcut ayarların yanında, EĞİTİM (ilk %70 gün) / TEST (son %30 gün) ayrımıyla ayar taraması yapar;
    en iyi ayarlar eğitimde seçilir, test dönemindeki sonuçları ayrıca gösterilir.

Çıktı: log + Telegram özeti + backtest_signals.csv.
Ortam değişkenleri: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (ops.), SAMPLE=2000 (0=tüm evren), DAYS=55,
COST_PCT=0.3, SEED=7, TICKERS=AAPL,TSLA (ops.)

Sınırlar: sadece bugün listelenen hisseler (delist olanlar yok), ~55 günlük tek bir piyasa dönemi, aynı gün
gelen sinyaller birbirine bağlı (piyasa günü etkisi). Sonuçlar yol göstericidir, kesin değildir.
"""
from __future__ import annotations

import dataclasses
import io
import os
import sys
import time
from datetime import timezone

import numpy as np
import pandas as pd

import scanner as sc

NY = "America/New_York"
OPEN_MIN = 9 * 60 + 30


# --------------------------------------------------------------------------- veri
def split_ohlcv(raw: pd.DataFrame | None, tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            raw = raw.swaplevel(axis=1)
        top = set(raw.columns.get_level_values(0))
        for t in tickers:
            if t in top and all(c in raw[t].columns for c in cols):
                out[t] = raw[t][cols]
    elif all(c in raw.columns for c in cols) and len(tickers) == 1:
        out[tickers[0]] = raw[cols]
    return out


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce").dropna()
    df = df[(df["Volume"] > 0) & (df["Close"] > 0)]
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(df.index)
    df.index = df.index.tz_localize(NY) if df.index.tz is None else df.index.tz_convert(NY)
    return sc.drop_incomplete(df, "15m")


def load_history(tickers: list[str], days: int, batch: int = 100) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    for s in range(0, len(tickers), batch):
        chunk = tickers[s:s + batch]
        raw = None
        for attempt in range(3):
            try:
                raw = yf.download(chunk, period=f"{days}d", interval="15m", group_by="ticker",
                                  auto_adjust=True, threads=True, progress=False)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  indirme hatası ({attempt + 1}/3): {exc}", file=sys.stderr)
                time.sleep(3 * (attempt + 1))
        for t, d in split_ohlcv(raw, chunk).items():
            d = prep(d)
            if len(d) >= 200:
                out[t] = d
        print(f"  {min(s + batch, len(tickers))}/{len(tickers)} indirildi, kullanılabilir {len(out)}", flush=True)
        time.sleep(1)
    return out


# --------------------------------------------------------------------------- simülasyon
def simulate(h, l, c, k0: int, end_k: int, entry: float, sl: float, tps, cost_pct: float) -> dict | None:
    """k0..end_k arası mumlarda SL/TP'leri yürüt. SL aynı mumda önce sayılır. Kalan parça end_k kapanışında çıkar.
    None = işlem alınmaz (stop üstünde değil / TP1 zaten geçilmiş / mum yok)."""
    risk = entry - sl
    if risk <= 0 or entry >= tps[0] or k0 > end_k:
        return None
    filled = [False, False, False]
    stopped = False
    for k in range(k0, end_k + 1):
        if l[k] <= sl:
            stopped = True
            break
        for j in range(3):
            if not filled[j] and h[k] >= tps[j]:
                filled[j] = True
        if filled[2]:
            break
    rest = (sl - entry) / risk if stopped else (c[end_k] - entry) / risk
    rr = [(tp - entry) / risk for tp in tps]
    cost_r = entry * cost_pct / 100 / risk
    r_a = (rr[0] if filled[0] else rest) - cost_r
    r_b = sum(rr[j] if filled[j] else rest for j in range(3)) / 3 - cost_r
    return {
        "tp1": filled[0], "tp2": filled[1], "tp3": filled[2],
        "slfirst": stopped and not filled[0],
        "timeout": (not stopped) and (not filled[0]),
        "rA": r_a, "rB": r_b,
    }


def run_modes(o, h, l, c, dates, eod, t: int, sl: float, tps, cost_pct: float) -> dict:
    """Sinyal mumu t için iki giriş varsayımı: 'o' = sonraki mum açılışı, 'c' = sonraki mum kapanışı."""
    res: dict = {}
    n = len(c)
    for mode in ("o", "c"):
        r = None
        if t + 1 < n and dates[t + 1] == dates[t]:
            if mode == "o":
                r = simulate(h, l, c, t + 1, eod[t + 1], float(o[t + 1]), sl, tps, cost_pct)
            elif t + 2 < n and dates[t + 2] == dates[t]:
                r = simulate(h, l, c, t + 2, eod[t + 2], float(c[t + 1]), sl, tps, cost_pct)
        if r is None:
            res[f"{mode}_skip"] = True
            for k in ("tp1", "tp2", "tp3", "slfirst", "timeout"):
                res[f"{mode}_{k}"] = False
            res[f"{mode}_rA"] = np.nan
            res[f"{mode}_rB"] = np.nan
        else:
            res[f"{mode}_skip"] = False
            for k, v in r.items():
                res[f"{mode}_{k}"] = v
    return res


# --------------------------------------------------------------------------- aday mumlar ve sinyaller
def loose_cfg(cfg: sc.Cfg) -> sc.Cfg:
    """Üretim ayarından daha gevşek: sonradan analizde sıkılaştırabilmek için geniş veri topla.
    Çizgi/düşüş şartları üretimle aynı kalır (aynı çizgi seçilsin)."""
    return dataclasses.replace(
        cfg, vol_mult=2.0, min_atr_pct=0.6, min_touches=2, min_range_atr=0.5,
        min_close_pos=0.5, min_bar_dollar_vol=10_000,
    )


def bar_stats(df: pd.DataFrame, cfg: sc.Cfg) -> dict:
    h, l, c, v = (df[k].to_numpy(float) for k in ("High", "Low", "Close", "Volume"))
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    atr = pd.Series(tr).rolling(cfg.atr_n).mean().shift(1).to_numpy()
    vbase = pd.Series(v).rolling(20).mean().shift(1).to_numpy()
    dollar = pd.Series(c * v).rolling(20).mean().to_numpy() * sc.BARS_PER_DAY.get(cfg.interval, 26)
    with np.errstate(divide="ignore", invalid="ignore"):
        st = {
            "atr": atr,
            "atr_pct": atr / c * 100,
            "vol_ratio": v / vbase,
            "close_pos": np.where(h > l, (c - l) / (h - l), 1.0),
            "range_atr": (h - l) / atr,
            "bar_dollar": c * v,
            "day_dollar": dollar,
        }
    dates = df.index.date
    st["dates"] = dates
    st["eod"] = pd.Series(np.arange(len(df))).groupby(dates).transform("max").to_numpy()
    # önceki (tamamlanmış) en fazla 5 seansın ortalama günlük aralığı % — scanner.avg_day_range_pct ile aynı tanım
    daily = df.groupby(dates).agg(H=("High", "max"), L=("Low", "min"), C=("Close", "last"))
    rp = (daily["H"] - daily["L"]) / daily["C"] * 100
    prev = rp.shift(1).rolling(5, min_periods=3).mean()
    st["day_range"] = pd.Series(dates).map(prev).to_numpy(float)
    st["mins"] = (df.index.hour * 60 + df.index.minute - OPEN_MIN).to_numpy()
    return st


def collect_for_ticker(sym: str, df: pd.DataFrame, cfg: sc.Cfg, cost_pct: float) -> list[dict]:
    lc = loose_cfg(cfg)
    st = bar_stats(df, cfg)
    n = len(df)
    idx = np.arange(n)
    c = df["Close"].to_numpy(float)
    cand = (
        (idx >= 60) & (idx < n - 1)
        & (c >= cfg.min_price)
        & (st["atr_pct"] >= lc.min_atr_pct)
        & (st["vol_ratio"] >= lc.vol_mult)
        & (st["bar_dollar"] >= lc.min_bar_dollar_vol)
        & (st["close_pos"] >= lc.min_close_pos)
        & (st["range_atr"] >= lc.min_range_atr)
        & (st["day_dollar"] >= cfg.min_dollar_vol)
    )
    o, h, l = (df[k].to_numpy(float) for k in ("Open", "High", "Low"))
    rows = []
    for t in np.flatnonzero(cand):
        window = df.iloc[max(0, t + 1 - cfg.lookback): t + 1]
        sig = sc.detect_breakout(window, lc)
        if not sig:
            continue
        row = {
            "symbol": sym, "ts": df.index[t], "date": st["dates"][t], "mins": int(st["mins"][t]),
            "price": float(c[t]), "atr_pct": sig["atr_pct"], "vol_ratio": sig["vol_ratio"],
            "day_range": float(st["day_range"][t]),
            "touches": sig["touches"], "span_bars": sig["span_bars"],
            "ext_pct": (c[t] / sig["line_level"] - 1) * 100, "close_pos": float(st["close_pos"][t]),
            "range_atr": float(st["range_atr"][t]), "bar_dollar": float(st["bar_dollar"][t]),
            "score": sig["score"], "kind": "sinyal",
        }
        row.update(run_modes(o, h, l, c, st["dates"], st["eod"], int(t), sig["sl"], sig["tps"], cost_pct))
        rows.append(row)
    return rows


def baseline_for_ticker(sym: str, df: pd.DataFrame, cfg: sc.Cfg, cost_pct: float,
                        rng: np.random.Generator, per_ticker: int = 12) -> list[dict]:
    """Rastgele mumlardan, aynı SL/TP kuralıyla (kırılım şartı olmadan) giriş."""
    lc = loose_cfg(cfg)
    st = bar_stats(df, cfg)
    n = len(df)
    idx = np.arange(n)
    c = df["Close"].to_numpy(float)
    # kıyas evreni = botun hisse filtreleri (hareketlilik, likidite); sadece kırılım ve hacim patlaması şartı yok
    ok = (
        (idx >= 60) & (idx < n - 2) & (c >= cfg.min_price)
        & (st["atr_pct"] >= cfg.min_atr_pct) & (st["bar_dollar"] >= cfg.min_bar_dollar_vol)
        & (st["day_dollar"] >= cfg.min_dollar_vol)
    )
    if cfg.min_day_range_pct > 0:
        ok &= st["day_range"] >= cfg.min_day_range_pct
    pool = np.flatnonzero(ok)
    if len(pool) == 0:
        return []
    pick = rng.choice(pool, size=min(per_ticker, len(pool)), replace=False)
    o, h, l = (df[k].to_numpy(float) for k in ("Open", "High", "Low"))
    rows = []
    for t in pick:
        atr = float(st["atr"][t])
        entry = float(c[t])
        risk = entry - (float(l[t]) - 0.25 * atr)
        risk = min(max(risk, 0.8 * atr), 2.5 * atr)
        sl = entry - risk
        if sl <= 0:
            continue
        tps = [entry + r * risk for r in cfg.tp_r]
        row = {
            "symbol": sym, "ts": df.index[t], "date": st["dates"][t], "mins": int(st["mins"][t]),
            "price": entry, "atr_pct": float(st["atr_pct"][t]), "vol_ratio": float(st["vol_ratio"][t]),
            "day_range": float(st["day_range"][t]), "kind": "rastgele",
        }
        row.update(run_modes(o, h, l, c, st["dates"], st["eod"], int(t), sl, tps, cost_pct))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- analiz
def apply_caps(df: pd.DataFrame, cfg: sc.Cfg) -> pd.DataFrame:
    """Üretimdeki sınırlar: hisse başı bekleme, tarama başı en fazla MAX_SIGNALS, günde en fazla MAX_DAILY."""
    if df.empty:
        return df
    keep = []
    last: dict[str, object] = {}
    per_day: dict[object, int] = {}
    for ts, grp in df.sort_values(["ts", "score"], ascending=[True, False]).groupby("ts", sort=True):
        taken = 0
        for ridx, r in grp.iterrows():
            if taken >= cfg.max_signals or per_day.get(r["date"], 0) >= cfg.max_daily:
                break
            prev = last.get(r["symbol"])
            if prev is not None and (r["date"] - prev).days < cfg.cooldown_days:
                continue
            keep.append(ridx)
            last[r["symbol"]] = r["date"]
            per_day[r["date"]] = per_day.get(r["date"], 0) + 1
            taken += 1
    return df.loc[keep]


def current_params(cfg: sc.Cfg) -> dict:
    return dict(vol=cfg.vol_mult, atr=cfg.min_atr_pct, touches=cfg.min_touches, skip_open=0,
                rng=cfg.min_range_atr, cpos=cfg.min_close_pos, dayr=cfg.min_day_range_pct)


def filt(df: pd.DataFrame, p: dict, cfg: sc.Cfg) -> pd.DataFrame:
    dayr = p.get("dayr", cfg.min_day_range_pct)  # ayar taramasında sabit: botun günlük hareket şartı
    m = (
        (df["vol_ratio"] >= p["vol"]) & (df["atr_pct"] >= p["atr"]) & (df["touches"] >= p["touches"])
        & (df["mins"] >= p["skip_open"]) & (df["range_atr"] >= p["rng"]) & (df["close_pos"] >= p["cpos"])
        & (df["bar_dollar"] >= cfg.min_bar_dollar_vol)
    )
    if dayr > 0:
        m &= df["day_range"] >= dayr  # nan (yeterli geçmiş yok) = elenir, scanner ile aynı
    return df[m]


def summarize(df: pd.DataFrame, mode: str = "o") -> dict:
    d = df[~df[f"{mode}_skip"].astype(bool)] if len(df) else df
    n = len(d)
    out = {"n": n, "skipped": len(df) - n}
    if n == 0:
        return out | {k: float("nan") for k in ("tp1", "tp2", "tp3", "sl", "timeout", "rA", "rB")}
    out |= {
        "tp1": d[f"{mode}_tp1"].astype(bool).mean() * 100,
        "tp2": d[f"{mode}_tp2"].astype(bool).mean() * 100,
        "tp3": d[f"{mode}_tp3"].astype(bool).mean() * 100,
        "sl": d[f"{mode}_slfirst"].astype(bool).mean() * 100,
        "timeout": d[f"{mode}_timeout"].astype(bool).mean() * 100,
        "rA": d[f"{mode}_rA"].mean(),
        "rB": d[f"{mode}_rB"].mean(),
    }
    return out


def fmt(label: str, s: dict) -> str:
    if s["n"] == 0:
        return f"{label}: işlem yok"
    return (f"{label}: n={s['n']} | TP1 %{s['tp1']:.0f} TP2 %{s['tp2']:.0f} TP3 %{s['tp3']:.0f} | "
            f"SL %{s['sl']:.0f} süre %{s['timeout']:.0f} | R(TP1'de çık) {s['rA']:+.2f} | R(1/3'er) {s['rB']:+.2f}")


BUCKETS = {
    "Saat (ET)": ("mins", [-1, 29, 89, 209, 329, 400], ["09:30-10:00", "10:00-11:00", "11:00-13:00", "13:00-15:00", "15:00+"]),
    "Hacim katı": ("vol_ratio", [0, 3, 4, 6, 1e9], ["2-3x", "3-4x", "4-6x", "6x+"]),
    "ATR %": ("atr_pct", [0, 1, 1.5, 2.5, 1e9], ["0.6-1", "1-1.5", "1.5-2.5", "2.5+"]),
    "Günlük ort. hareket %": ("day_range", [0, 5, 8, 12, 1e9], ["<5", "5-8", "8-12", "12+"]),
    "Dokunuş": ("touches", [1, 2, 3, 1e9], ["2", "3", "4+"]),
    "Fiyat $": ("price", [0, 1, 3, 10, 1e9], ["<1", "1-3", "3-10", "10+"]),
    "Çizgiden uzaklık %": ("ext_pct", [0, 0.6, 1.2, 2.5, 1e9], ["0.3-0.6", "0.6-1.2", "1.2-2.5", "2.5+"]),
}


def bucket_table(df: pd.DataFrame, min_n: int = 25) -> list[str]:
    lines = []
    for title, (col, edges, labels) in BUCKETS.items():
        cut = pd.cut(df[col], bins=edges, labels=labels)
        parts = []
        for lab in labels:
            g = df[cut == lab]
            s = summarize(g)
            if s["n"] >= min_n:
                parts.append(f"{lab}: n={s['n']} TP1 %{s['tp1']:.0f} R{s['rB']:+.2f}")
        if parts:
            lines.append(f"• {title}: " + " | ".join(parts))
    return lines


def grid_search(loose: pd.DataFrame, cfg: sc.Cfg, min_n: int = 30) -> tuple[list, str]:
    dates = sorted(loose["date"].unique())
    if len(dates) < 10:
        return [], "yeterli gün yok"
    cut_date = dates[int(len(dates) * 0.7)]
    train, test = loose[loose["date"] < cut_date], loose[loose["date"] >= cut_date]
    rows = []
    for vol in (2.0, 3.0, 4.0):
        for atr in (0.6, 1.0, 1.5, 2.0):
            for touches in (2, 3, 4):
                for skip in (0, 30):
                    for rng in (0.5, 1.0, 1.5):
                        for cpos in (0.5, 0.7, 0.85):
                            p = dict(vol=vol, atr=atr, touches=touches, skip_open=skip, rng=rng, cpos=cpos)
                            tr = summarize(apply_caps(filt(train, p, cfg), cfg))
                            if tr["n"] < min_n:
                                continue
                            te = summarize(apply_caps(filt(test, p, cfg), cfg))
                            rows.append((tr["rB"], p, tr, te))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:3], f"eğitim: {dates[0]} - {dates[int(len(dates) * 0.7) - 1]}, test: {cut_date} - {dates[-1]}"


def pdesc(p: dict) -> str:
    return (f"hacim≥{p['vol']:g}x, ATR≥%{p['atr']:g}, dokunuş≥{p['touches']}, "
            f"açılış {p['skip_open']} dk atla, mum≥{p['rng']:g} ATR, kapanış≥{p['cpos']:g}")


def build_report(loose: pd.DataFrame, base: pd.DataFrame, cfg: sc.Cfg, n_tickers: int, days: int,
                 cost_pct: float) -> list[str]:
    cur_p = current_params(cfg)
    cur = apply_caps(filt(loose, cur_p, cfg), cfg)
    lines = [
        f"📊 Backtest — 15 dk, son {days} gün, {n_tickers} hisse, maliyet %{cost_pct:g}",
        "R(TP1'de çık): TP1'de hepsini sat | R(1/3'er): TP1/TP2/TP3'te üçte birini sat. SL aynı mumda önce sayılır.",
        "",
        "MEVCUT AYARLAR (sonraki mum açılışında giriş):",
        fmt("Sinyaller", summarize(cur)),
        fmt("Rastgele giriş (kıyas)", summarize(base)),
        fmt("Gecikmeli giriş (sonraki mum kapanışı)", summarize(cur, "c")),
    ]
    s, b = summarize(cur), summarize(base)
    if s["n"] and b["n"]:
        diff = s["tp1"] - b["tp1"]
        lines.append(f"→ Sinyalin TP1 üstünlüğü rastgeleye göre: {diff:+.0f} puan; R farkı (1/3'er): {s['rB'] - b['rB']:+.2f}")
    if s["n"] and s["n"] < 40:
        lines.append(f"⚠ Sadece {s['n']} işlem var; sonuç güvenilir değil.")
    lines += ["", "DİLİMLER (gevşek ayarlı geniş sinyal kümesi, mevcut sınırlar uygulanmadan; n≥25 olanlar):"]
    lines += bucket_table(loose) or ["(yeterli veri yok)"]
    top, note = grid_search(loose, cfg)
    lines += ["", f"AYAR TARAMASI — eğitimde seçilir, testte sınanır ({note}):"]
    if not top:
        lines.append("(yeterli işlem yok)")
    for i, (_, p, tr, te) in enumerate(top, 1):
        lines.append(f"{i}) {pdesc(p)}")
        lines.append("   " + fmt("eğitim", tr))
        lines.append("   " + fmt("TEST  ", te))
    if top:
        lines.append("Test satırları eğitimden belirgin kötüyse o ayar sadece geçmişe uymuştur; kullanma.")
    return lines


def send_report(lines: list[str], token: str, chat_id: str) -> None:
    text = "\n".join(lines)
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if len(cur) + len(ln) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        # HTML modu kapalı: düz metin (hisse/ayar metinlerinde < > karakterleri bozmasın)
        import requests
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": ch, "disable_web_page_preview": True}, timeout=20)
        except requests.RequestException as exc:
            print(f"Telegram hatası: {exc}", file=sys.stderr)
        time.sleep(1)


# --------------------------------------------------------------------------- ana akış
def run(histories: dict[str, pd.DataFrame], cfg: sc.Cfg, cost_pct: float, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    sig_rows, base_rows = [], []
    for k, (sym, df) in enumerate(histories.items(), 1):
        sig_rows += collect_for_ticker(sym, df, cfg, cost_pct)
        base_rows += baseline_for_ticker(sym, df, cfg, cost_pct, rng)
        if k % 200 == 0:
            print(f"  {k}/{len(histories)} hisse işlendi, {len(sig_rows)} aday sinyal", flush=True)
    loose = pd.DataFrame(sig_rows)
    base = pd.DataFrame(base_rows)
    if len(base) > 3000:
        base = base.sample(3000, random_state=seed)
    return loose, base


def main() -> int:
    os.environ.setdefault("INTERVAL", "15m")
    cfg = sc.load_cfg()
    if cfg.interval != "15m":
        print("Bu backtest 15 dakikalık mum için yazıldı (INTERVAL=15m).", file=sys.stderr)
        return 1
    days = int(float(os.getenv("DAYS", "55") or 55))
    cost_pct = float(os.getenv("COST_PCT", "0.3") or 0.3)
    seed = int(float(os.getenv("SEED", "7") or 7))
    sample = int(float(os.getenv("SAMPLE", "2000") or 2000))
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if os.getenv("TICKERS", "").strip():
        tickers = [t.strip().upper() for t in os.environ["TICKERS"].split(",") if t.strip()]
    else:
        print("Hisse evreni indiriliyor...", flush=True)
        tickers = sc.load_universe(cfg.universe)
        if sample and sample < len(tickers):
            tickers = sorted(np.random.default_rng(seed).choice(tickers, size=sample, replace=False).tolist())
    print(f"{len(tickers)} hisse, {days} gün, 15 dk.", flush=True)

    hist = load_history(tickers, days)
    loose, base = run(hist, cfg, cost_pct, seed)
    if loose.empty:
        print("Hiç aday sinyal çıkmadı.")
        return 0
    loose.to_csv("backtest_signals.csv", index=False)
    lines = build_report(loose, base, cfg, len(hist), days, cost_pct)
    print("\n".join(lines))
    with open("backtest_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    if token and chat_id:
        send_report(lines, token, chat_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
