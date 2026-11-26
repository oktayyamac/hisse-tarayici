#!/usr/bin/env python3
"""
Düşen Trend Kırılım Tarayıcısı (ABD hisseleri) -> Telegram

Ne yapar?
  1. ABD hisse evrenini (Nasdaq Trader listesi) çeker, ETF / warrant / test sembollerini eler.
  2. Günlük (veya saatlik) mumları yfinance ile indirir.
  3. Her hissede düşen direnç çizgisini (alçalan tepeleri birleştiren trend çizgisi) bulur
     ve SON KAPANMIŞ mumun bu çizgiyi yukarı kırıp kırmadığına bakar.
  4. Kırılım varsa giriş / SL / TP1-TP2-TP3 hesaplar ve Telegram'a gönderir.

Kırılım sayılma şartları (hepsi ayarlanabilir, bkz. load_cfg ve PRESETS):
  - Çizgi en az 3 tepeye dokunur (MIN_TOUCHES), arada tepeler çizginin üstüne çıkmamış olmalı
  - Çizginin iki tepesi arası düşüş en az MIN_DROP_PCT ve en az 2 ATR (gürültüden büyük)
  - Önceki mum çizginin üstünde değil -> kırılım "taze" (ilk kırılım mumu)
  - Kırılım mumu çizginin üstünde kapanır, mum aralığının üst %30'unda kapanır
  - Kırılım mumu "genişleme mumu" olmalı: boyu en az 1 ATR (MIN_RANGE_ATR)
  - Hacim, son 20 mum ortalamasının en az VOL_MULT katı (15m için 3x)
  - Hareketlilik: ATR/fiyat en az MIN_ATR_PCT (15m için %1.5) ve son 5 seansın ortalama günlük
    hareketi (yüksek-düşük/kapanış) en az MIN_DAY_RANGE_PCT (gün içi mumlar için %5); hareketsiz hisseler elenir
  - Kırılım mumunda en az MIN_BAR_DOLLAR_VOL kadar $ işlem (15m için 25K$)
  - Sıralama ve üst sınır: her taramada en fazla MAX_SIGNALS (6), günde en fazla MAX_DAILY (20) sinyal
  - Likidite filtresi: fiyat >= 0.20$, tahmini günlük işlem hacmi >= 250K$ (MIN_PRICE / MIN_DOLLAR_VOL)

Zaman dilimi: varsayılan 15 DAKİKALIK mum (INTERVAL=15m). Diğerleri için INTERVAL=5m / 30m / 1h / 1d
verilir; her biri için uygun varsayılanlar PRESETS içinde (backtest edilmedi, env ile ezilebilir).

Risk / hedefler:
  SL  = min(kırılım mumunun dibi, çizgi) - 0.25*ATR  (risk 0.8-2.5 ATR arasına sıkıştırılır)
  TPn = Giriş + n_R * risk   (varsayılan 1R / 2R / 3R, TP_R ile değişir)

Kullanım:
  python scanner.py                       # tüm evreni tara, Telegram'a gönder
  python scanner.py --dry-run             # Telegram'a göndermeden ekrana yaz
  python scanner.py --tickers AAPL,TSLA   # sadece bu hisseleri tara
  python scanner.py --test-telegram       # bot bağlantısını dene

Ortam değişkenleri: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (+ isteğe bağlı ayarlar)
Bu bir yatırım tavsiyesi değildir; sinyallerin geçmiş başarısı test edilmemiştir.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
BAD_NAME = re.compile(r"warrant|\brights?\b|\bunits?\b|preferred|notes? due|depositary|% ", re.I)
GOOD_SYMBOL = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")
INTERVAL_DELTA = {"1m": "1min", "2m": "2min", "5m": "5min", "15m": "15min", "30m": "30min", "60m": "1h", "1h": "1h"}
BARS_PER_DAY = {"1d": 1, "1h": 6.5, "60m": 6.5, "30m": 13, "15m": 26, "5m": 78, "2m": 195, "1m": 390}
# Zaman dilimine göre varsayılanlar. yfinance sınırları: 1m en fazla ~7 gün, 2m-30m en fazla 60 gün.
PRESETS = {
    "1d": dict(period="10mo", lookback=100, min_drop_pct=3.0, line_tol_pct=1.0, break_buf_pct=0.5, max_ext_pct=8.0,
               vol_mult=2.0, min_atr_pct=3.0, min_range_atr=1.0, min_drop_atr=2.0, min_bar_dollar_vol=250_000,
               min_day_range_pct=0.0),
    "1h": dict(period="60d", lookback=120, min_drop_pct=2.0, line_tol_pct=0.8, break_buf_pct=0.4, max_ext_pct=6.0,
               vol_mult=2.5, min_atr_pct=1.8, min_range_atr=1.0, min_drop_atr=2.0, min_bar_dollar_vol=75_000,
               min_day_range_pct=5.0),
    "30m": dict(period="30d", lookback=150, min_drop_pct=1.5, line_tol_pct=0.6, break_buf_pct=0.3, max_ext_pct=5.0,
                vol_mult=3.0, min_atr_pct=1.3, min_range_atr=1.0, min_drop_atr=2.0, min_bar_dollar_vol=40_000,
                min_day_range_pct=5.0),
    "15m": dict(period="10d", lookback=150, min_drop_pct=1.0, line_tol_pct=0.5, break_buf_pct=0.3, max_ext_pct=4.0,
                vol_mult=3.0, min_atr_pct=1.5, min_range_atr=1.0, min_drop_atr=2.0, min_bar_dollar_vol=25_000,
                min_day_range_pct=5.0),
    "5m": dict(period="10d", lookback=150, min_drop_pct=0.8, line_tol_pct=0.4, break_buf_pct=0.2, max_ext_pct=3.0,
               vol_mult=3.0, min_atr_pct=0.7, min_range_atr=1.0, min_drop_atr=2.0, min_bar_dollar_vol=10_000,
               min_day_range_pct=5.0),
    "1m": dict(period="5d", lookback=200, min_drop_pct=0.5, line_tol_pct=0.3, break_buf_pct=0.15, max_ext_pct=2.0,
               vol_mult=3.0, min_atr_pct=0.4, min_range_atr=1.0, min_drop_atr=2.0, min_bar_dollar_vol=3_000,
               min_day_range_pct=5.0),
}


# --------------------------------------------------------------------------- ayarlar
@dataclass
class Cfg:
    interval: str
    period: str
    lookback: int
    pivot_k: int
    min_span: int
    max_anchor_age: int
    min_drop_pct: float
    min_touches: int
    line_tol_pct: float
    break_buf_pct: float
    max_ext_pct: float
    vol_mult: float
    min_close_pos: float
    min_price: float
    min_dollar_vol: float
    min_atr_pct: float
    min_range_atr: float
    min_drop_atr: float
    min_bar_dollar_vol: float
    min_day_range_pct: float
    tp_r: tuple
    atr_n: int
    max_signals: int
    max_daily: int
    cooldown_days: int
    batch: int
    universe: str
    sent_file: str


def _env(name: str, default):
    """Ortam değişkeni; tanımsız veya boşsa (GitHub'da tanımsız variable boş gelir) varsayılan."""
    v = os.getenv(name)
    return default if v is None or not v.strip() else v.strip()


def _f(name: str, default: float) -> float:
    return float(_env(name, default))


def _i(name: str, default: int) -> int:
    return int(float(_env(name, default)))


def _s(name: str, default: str) -> str:
    return str(_env(name, default))


def load_cfg() -> Cfg:
    tp = tuple(float(x) for x in _s("TP_R", "1,2,3").split(",") if x.strip())
    if len(tp) < 3:
        tp = (1.0, 2.0, 3.0)
    interval = _s("INTERVAL", "15m").lower()
    p = PRESETS.get("1h" if interval == "60m" else interval, PRESETS["15m"])
    return Cfg(
        interval=interval,
        period=_s("PERIOD", p["period"]),
        lookback=_i("LOOKBACK", p["lookback"]),  # kaç mum geriye bakılsın
        pivot_k=_i("PIVOT_K", 3),                # swing tepesi için sağ/sol mum sayısı
        min_span=_i("MIN_SPAN", 10),             # iki tepe arası en az mum
        max_anchor_age=_i("MAX_ANCHOR_AGE", 60), # ikinci tepe en fazla kaç mum önce
        min_drop_pct=_f("MIN_DROP_PCT", p["min_drop_pct"]),  # iki tepe arası en az düşüş %
        min_touches=_i("MIN_TOUCHES", 3),        # çizgiye en az bu kadar tepe dokunmuş olsun (2 nokta her zaman çizgi verir)
        line_tol_pct=_f("LINE_TOL_PCT", p["line_tol_pct"]),    # tepelerin çizgiyi aşabileceği tolerans %
        break_buf_pct=_f("BREAK_BUF_PCT", p["break_buf_pct"]),  # kapanış çizginin en az bu kadar üstünde
        max_ext_pct=_f("MAX_EXT_PCT", p["max_ext_pct"]),        # çizgiden aşırı uzaklaşmışsa (kovalama) alma
        vol_mult=_f("VOL_MULT", p["vol_mult"]),  # kırılım mumu hacmi / son 20 mum ortalaması
        min_close_pos=_f("MIN_CLOSE_POS", 0.7),  # kapanış mum aralığının üst %30'unda olsun
        min_price=_f("MIN_PRICE", 0.20),
        min_dollar_vol=_f("MIN_DOLLAR_VOL", 250_000),  # tahmini günlük $ hacim; 0 yaparsan filtre kalkar
        min_atr_pct=_f("MIN_ATR_PCT", p["min_atr_pct"]),        # ATR/fiyat bunun altındaysa hisse "hareketsiz"
        min_range_atr=_f("MIN_RANGE_ATR", p["min_range_atr"]),  # kırılım mumu en az bu kadar ATR boyunda
        min_drop_atr=_f("MIN_DROP_ATR", p["min_drop_atr"]),     # çizginin iki tepesi arası düşüş en az bu kadar ATR
        min_bar_dollar_vol=_f("MIN_BAR_DOLLAR_VOL", p["min_bar_dollar_vol"]),  # kırılım mumunda en az bu kadar $ işlem
        # son 5 seansın ortalama günlük aralığı (yüksek-düşük)/kapanış % bunun altındaysa hisse "hareketsiz"; 0 = kapalı
        min_day_range_pct=_f("MIN_DAY_RANGE_PCT", p["min_day_range_pct"]),
        tp_r=tp[:3],
        atr_n=_i("ATR_N", 14),
        max_signals=_i("MAX_SIGNALS", 6),        # her taramada en fazla kaç sinyal
        max_daily=_i("MAX_DAILY", 20),           # bir günde toplam en fazla kaç sinyal
        cooldown_days=_i("COOLDOWN_DAYS", 5),
        batch=_i("BATCH", 200),
        universe=_s("UNIVERSE", "all").lower(),  # all | nasdaq
        sent_file=_s("SENT_FILE", "sent_signals.json"),
    )


# --------------------------------------------------------------------------- evren
def _fetch_symbol_file(url: str) -> pd.DataFrame:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    # keep_default_na=False: "NA", "NAN" gibi gerçek hisse kodları boş değer sanılmasın
    df = pd.read_csv(io.StringIO(r.text), sep="|", dtype=str, keep_default_na=False)
    first = df.columns[0]
    return df[~df[first].astype(str).str.startswith("File Creation Time")].copy()


def _clean_symbols(df: pd.DataFrame, sym_col: str) -> list[str]:
    df = df.copy()
    df = df[df["Test Issue"] != "Y"]
    df = df[df["ETF"] != "Y"]
    df = df[~df["Security Name"].str.contains(BAD_NAME)]
    syms = df[sym_col].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False)
    return [s for s in syms if isinstance(s, str) and GOOD_SYMBOL.match(s)]


def load_universe(kind: str) -> list[str]:
    syms: list[str] = []
    nas = _fetch_symbol_file(NASDAQ_URL)
    syms += _clean_symbols(nas, "Symbol")
    if kind != "nasdaq":
        oth = _fetch_symbol_file(OTHER_URL)
        syms += _clean_symbols(oth, "ACT Symbol")
    return sorted(set(syms))


# --------------------------------------------------------------------------- veri
def download_batch(tickers: list[str], cfg: Cfg) -> Optional[pd.DataFrame]:
    import yfinance as yf  # tembel import: test ortamında gerekmez

    for attempt in range(3):
        try:
            return yf.download(
                tickers,
                period=cfg.period,
                interval=cfg.interval,
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  indirme hatası ({attempt + 1}/3): {exc}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return None


def split_frames(raw: Optional[pd.DataFrame], tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    cols = ["High", "Low", "Close", "Volume"]
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):  # alan adı üstte ise çevir
            raw = raw.swaplevel(axis=1)
        top = set(raw.columns.get_level_values(0))
        for t in tickers:
            if t in top:
                sub = raw[t]
                if all(c in sub.columns for c in cols):
                    out[t] = sub[cols]
    elif all(c in raw.columns for c in cols) and len(tickers) == 1:
        out[tickers[0]] = raw[cols]
    return out


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    return df.dropna()


def drop_incomplete(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Henüz kapanmamış son mumu at (kırılım sadece kapanmış mumda sayılır)."""
    if df.empty:
        return df
    now_utc = pd.Timestamp.now(tz="UTC")
    ts = pd.Timestamp(df.index[-1])
    if interval == "1d":
        now_ny = now_utc.tz_convert(ZoneInfo("America/New_York"))
        if ts.date() == now_ny.date() and (now_ny.hour, now_ny.minute) < (16, 5):
            return df.iloc[:-1]
        return df
    dur = pd.Timedelta(INTERVAL_DELTA.get(interval, "1h"))
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York")
    if ts + dur > now_utc:
        return df.iloc[:-1]
    return df


def avg_day_range_pct(df: pd.DataFrame, interval: str, n: int = 5, min_days: int = 3) -> float:
    """Önceki (tamamlanmış) n seansın ortalama günlük aralığı: (en yüksek - en düşük) / kapanış * 100.
    Bugünkü (son mumun olduğu) seans hariç tutulur. Yeterli seans yoksa nan döner."""
    if df.empty:
        return float("nan")
    if interval == "1d":
        g = df.iloc[:-1].tail(n)
        rp = (g["High"] - g["Low"]) / g["Close"] * 100
    else:
        dates = pd.DatetimeIndex(df.index).date
        daily = df.groupby(dates).agg(H=("High", "max"), L=("Low", "min"), C=("Close", "last"))
        rp = ((daily["H"] - daily["L"]) / daily["C"] * 100).iloc[:-1].tail(n)
    return float(rp.mean()) if len(rp) >= min_days else float("nan")


def prefilter(df: pd.DataFrame, cfg: Cfg) -> bool:
    if len(df) < max(60, cfg.atr_n + 30):
        return False
    if df["Close"].iloc[-1] < cfg.min_price:
        return False
    per_bar = (df["Close"] * df["Volume"]).iloc[-20:].mean()
    daily_est = per_bar * BARS_PER_DAY.get(cfg.interval, 1)  # mum başına $ hacmi -> günlük tahmin
    if daily_est < cfg.min_dollar_vol:
        return False
    if cfg.min_day_range_pct > 0:  # hareketsiz hisseleri ele (geçmiş günlük hareketi yetersiz veya bilinmiyor)
        dr = avg_day_range_pct(df, cfg.interval)
        if not (dr >= cfg.min_day_range_pct):
            return False
    return True


# --------------------------------------------------------------------------- tespit
def find_pivot_highs(high: np.ndarray, k: int) -> list[int]:
    """Solundaki k ve sağındaki k mumdan yüksek olan swing tepeleri."""
    piv = []
    for i in range(k, len(high) - k):
        if high[i] > high[i - k:i].max() and high[i] >= high[i + 1:i + k + 1].max():
            piv.append(i)
    return piv


def detect_breakout(df: pd.DataFrame, cfg: Cfg) -> Optional[dict]:
    d = df.iloc[-cfg.lookback:]
    n = len(d)
    if n < max(40, cfg.atr_n + 25):
        return None
    hi = d["High"].to_numpy(float)
    lo = d["Low"].to_numpy(float)
    cl = d["Close"].to_numpy(float)
    vol = d["Volume"].to_numpy(float)
    last = n - 1
    tol = cfg.line_tol_pct / 100
    buf = cfg.break_buf_pct / 100
    ext = cfg.max_ext_pct / 100

    # ATR (kırılım mumu hariç son atr_n mum): hareketlilik filtreleri için önce hesaplanır
    prev_c = cl[:-1]
    tr = np.maximum.reduce([hi[1:] - lo[1:], np.abs(hi[1:] - prev_c), np.abs(lo[1:] - prev_c)])
    atr = float(tr[-(cfg.atr_n + 1):-1].mean())
    if not np.isfinite(atr) or atr <= 0:
        return None
    if atr / cl[last] * 100 < cfg.min_atr_pct:          # hareketsiz hisse
        return None
    if cl[last] * vol[last] < cfg.min_bar_dollar_vol:   # kırılım mumunda gerçek para girişi yok
        return None

    pivs = find_pivot_highs(hi, cfg.pivot_k)[-8:]
    best = None
    for a in range(len(pivs)):
        for b in range(a + 1, len(pivs)):
            i, j = pivs[a], pivs[b]
            if j - i < cfg.min_span or last - j > cfg.max_anchor_age:
                continue
            if hi[j] >= hi[i] or (hi[i] - hi[j]) / hi[i] * 100 < cfg.min_drop_pct:
                continue
            if hi[i] - hi[j] < cfg.min_drop_atr * atr:   # düşüş normal gürültüden büyük olmalı
                continue
            slope = (hi[j] - hi[i]) / (j - i)
            line = hi[i] + slope * (np.arange(i, last + 1) - i)  # i..last
            if line[-1] <= 0:
                continue
            seg_line = line[:-1]
            if np.any(hi[i:last] > seg_line * (1 + tol)):       # arada çizgiyi aşan tepe var
                continue
            if cl[last - 1] > line[-2]:                          # kırılım taze değil
                continue
            lvl = line[-1]
            if cl[last] <= lvl * (1 + buf) or cl[last] > lvl * (1 + ext):
                continue
            touches = sum(
                1 for p in pivs
                if i <= p < last and abs(hi[p] - line[p - i]) <= line[p - i] * tol
            )
            if touches < cfg.min_touches:
                continue
            key = (touches, j - i)
            if best is None or key > best[0]:
                best = (key, i, j, lvl, touches)
    if best is None:
        return None
    _, i, j, lvl, touches = best

    base_vol = vol[last - 20:last].mean()
    vol_ratio = vol[last] / base_vol if base_vol > 0 else 0.0
    if vol_ratio < cfg.vol_mult:
        return None
    rng = hi[last] - lo[last]
    close_pos = (cl[last] - lo[last]) / rng if rng > 0 else 1.0
    if close_pos < cfg.min_close_pos:
        return None
    if rng < cfg.min_range_atr * atr:                    # kırılım mumu "genişleme" mumu olmalı
        return None

    entry = float(cl[last])
    risk = entry - (min(lo[last], lvl) - 0.25 * atr)
    risk = min(max(risk, 0.8 * atr), 2.5 * atr)
    sl = entry - risk
    if sl <= 0:
        return None
    tps = [entry + r * risk for r in cfg.tp_r]
    return {
        "entry": entry,
        "sl": float(sl),
        "tps": [float(x) for x in tps],
        "line_level": float(lvl),
        "touches": int(touches),
        "span_bars": int(last - i),
        "anchor1": str(pd.Timestamp(d.index[i]).date()),
        "anchor2": str(pd.Timestamp(d.index[j]).date()),
        "vol_ratio": float(vol_ratio),
        "atr_pct": float(atr / entry * 100),
        # sıralama: hacim patlaması x çizgi kalitesi x hareketlilik (az sinyal gönderirken en iyiler öne geçsin)
        "score": float(vol_ratio * min(touches, 4) * (atr / entry * 100) ** 0.5),
    }


# --------------------------------------------------------------------------- mesaj
def fmt_price(p: float) -> str:
    return f"{p:,.2f}" if p >= 1 else f"{p:.4f}"


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def build_message(s: dict, cfg: Cfg) -> str:
    sym = html.escape(s["symbol"])
    tf = "günlük" if cfg.interval == "1d" else cfg.interval
    e = s["entry"]
    lines = [
        f"🟢 <b>{sym}</b> — düşen trend kırılımı ({tf})",
        f"Giriş: <b>{fmt_price(e)}</b>",
        f"🛑 SL: {fmt_price(s['sl'])} ({pct(s['sl'], e):+.1f}%)",
    ]
    for k, (tp, r) in enumerate(zip(s["tps"], cfg.tp_r), 1):
        lines.append(f"🎯 TP{k}: {fmt_price(tp)} ({pct(tp, e):+.1f}%) · {r:g}R")
    lines.append(
        f"📐 {s['touches']} dokunuşlu çizgi · {s['span_bars']} mum · "
        f"kırılan seviye {fmt_price(s['line_level'])}"
    )
    stats = f"📊 Hacim {s['vol_ratio']:.1f}x ort. · ATR %{s['atr_pct']:.1f}"
    dr = s.get("day_range_pct")
    if dr is not None and dr == dr:  # nan değilse
        stats += f" · günlük ort. hareket %{dr:.0f}"
    lines.append(stats)
    tv = html.escape(s["symbol"].replace("-", "."))
    lines.append(f'<a href="https://www.tradingview.com/symbols/{tv}/">Grafik</a>')
    return "\n".join(lines)


def tg_send(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if not r.ok:
            print(f"Telegram hatası {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return r.ok
    except requests.RequestException as exc:
        print(f"Telegram bağlantı hatası: {exc}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- tekrar önleme
def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    cutoff = date.today().toordinal() - 30
    state = {
        k: v for k, v in state.items()
        if date.fromisoformat(v.get("sent", "1970-01-01")).toordinal() >= cutoff
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def pick_to_send(signals: list[dict], state: dict, cfg: Cfg, today: Optional[date] = None) -> list[dict]:
    """Yeni sinyallerden, tarama başı ve günlük üst sınırı aşmayacak kadarını (en yüksek skorlular) seç."""
    today = today or datetime.now(timezone.utc).date()
    sent_today = sum(1 for v in state.values() if v.get("sent") == today.isoformat())
    room = max(0, min(cfg.max_signals, cfg.max_daily - sent_today))
    return filter_new(signals, state, cfg, today)[:room]


def filter_new(signals: list[dict], state: dict, cfg: Cfg, today: Optional[date] = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    out = []
    for s in signals:
        prev = state.get(s["symbol"])
        if prev:
            if prev.get("bar") == s["bar_date"]:
                continue
            try:
                age = (today - date.fromisoformat(prev["sent"])).days
            except (KeyError, ValueError):
                age = 999
            if age < cfg.cooldown_days:
                continue
        out.append(s)
    return out


# --------------------------------------------------------------------------- ana akış
def scan(tickers: list[str], cfg: Cfg) -> tuple[list[dict], int]:
    signals: list[dict] = []
    scanned = 0
    total = len(tickers)
    for start in range(0, total, cfg.batch):
        batch = tickers[start:start + cfg.batch]
        frames = split_frames(download_batch(batch, cfg), batch)
        for t, raw_df in frames.items():
            df = drop_incomplete(clean(raw_df), cfg.interval)
            if not prefilter(df, cfg):
                continue
            scanned += 1
            sig = detect_breakout(df, cfg)
            if sig:
                sig["symbol"] = t
                sig["bar_date"] = str(df.index[-1])
                sig["day_range_pct"] = avg_day_range_pct(df, cfg.interval)
                signals.append(sig)
        print(f"  {min(start + cfg.batch, total)}/{total} hisse işlendi, {len(signals)} sinyal", flush=True)
        time.sleep(1)
    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals, scanned


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", help="Virgülle ayrılmış liste (örn. AAPL,TSLA). Boşsa tüm evren.")
    ap.add_argument("--dry-run", action="store_true", help="Telegram'a gönderme, ekrana yaz.")
    ap.add_argument("--test-telegram", action="store_true", help="Sadece bir test mesajı gönder.")
    args = ap.parse_args()

    cfg = load_cfg()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    can_send = bool(token and chat_id) and not args.dry_run

    if args.test_telegram:
        if not (token and chat_id):
            print("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlı değil.", file=sys.stderr)
            return 1
        ok = tg_send(token, chat_id, "✅ Düşen trend kırılım tarayıcısı bağlantı testi")
        return 0 if ok else 1

    if args.tickers or os.getenv("TICKERS"):
        raw = args.tickers or os.getenv("TICKERS", "")
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    else:
        print("Hisse evreni indiriliyor...", flush=True)
        try:
            tickers = load_universe(cfg.universe)
        except requests.RequestException as exc:
            print(f"Evren indirilemedi: {exc}. --tickers ile liste verebilirsin.", file=sys.stderr)
            return 1
    print(f"{len(tickers)} sembol taranacak ({cfg.interval} mumlar).", flush=True)

    signals, scanned = scan(tickers, cfg)
    state = load_state(cfg.sent_file)
    fresh = pick_to_send(signals, state, cfg)
    print(f"Taranan: {scanned} | kırılım: {len(signals)} | gönderilecek sinyal: {len(fresh)}")

    if not fresh:
        if can_send and os.getenv("SEND_EMPTY", "0") == "1":
            tg_send(token, chat_id, f"📡 Tarama bitti ({scanned} hisse): yeni düşen trend kırılımı yok.")
        return 0

    header = (
        f"📡 <b>Düşen trend kırılımı taraması</b> — {datetime.now(timezone.utc):%Y-%m-%d}\n"
        f"{len(fresh)} yeni sinyal ({scanned} hisse tarandı). Yatırım tavsiyesi değildir."
    )
    if cfg.interval == "1d":  # 15 dakikalıkta her taramada başlık mesajı gürültü olur, sadece günlükte gönder
        if can_send:
            tg_send(token, chat_id, header)
        else:
            print("\n" + re.sub(r"<[^>]+>", "", header))

    today = datetime.now(timezone.utc).date().isoformat()
    for s in fresh:
        msg = build_message(s, cfg)
        if can_send:
            if tg_send(token, chat_id, msg):
                state[s["symbol"]] = {"bar": s["bar_date"], "sent": today}
            time.sleep(1)
        else:
            print("\n" + re.sub(r"<[^>]+>", "", msg))

    if can_send:
        save_state(cfg.sent_file, state)
    elif not args.dry_run:
        print("\n(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı olmadığı için mesajlar ekrana yazıldı.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
