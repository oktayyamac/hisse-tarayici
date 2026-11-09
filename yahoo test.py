#!/usr/bin/env python3
"""
Yahoo seans öncesi veri denemesi.

Yahoo'nun 5 dakikalık verisinin seans öncesini (New York 04:00'dan itibaren) verip vermediğini,
ne kadar gecikmeli olduğunu ve hacmin Alpaca IEX verisine göre nasıl durduğunu ölçer.
Sonucu Telegram'a gönderir. Ek paket gerekmez. Aynı klasörde scanner.py olmalı.

Ayar (isteğe bağlı): YF_SYMBOLS (virgülle ayrılmış, varsayılan OMH,ELPW,AAPL)
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import scanner as sc

ET = ZoneInfo("America/New_York")
SYMS = [s.strip().upper() for s in (os.environ.get("YF_SYMBOLS") or "OMH,ELPW,AAPL").split(",") if s.strip()]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def yahoo_bars(sym):
    """Döner: (mumlar, hata). Mum: (epoch sn, o, h, l, c, hacim)."""
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
            time.sleep(2)
            continue
        except Exception as e:
            err = str(e)[:80]
            continue
        chart = data.get("chart") or {}
        res = chart.get("result")
        if not res:
            err = str(chart.get("error"))[:120]
            continue
        r0 = res[0]
        ts = r0.get("timestamp") or []
        q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
        cols = {k: q.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close", "volume")}
        bars = []
        for i, t in enumerate(ts):
            c = cols["close"][i]
            if c is None:
                continue
            bars.append((t, cols["open"][i], cols["high"][i], cols["low"][i], c, cols["volume"][i] or 0))
        return bars, None
    return [], err or "bilinmeyen hata"


def alpaca_bars(sym, day):
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime(day.year, day.month, day.day, 20, 0, tzinfo=ET).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"symbols": sym, "timeframe": "5Min", "start": start, "end": end, "limit": 10000,
              "feed": "iex", "adjustment": "raw"}
    data = sc.http_json(sc.DATA_URL + "/v2/stocks/bars?" + urllib.parse.urlencode(params), sc.alpaca_headers())
    out = []
    for b in (data.get("bars") or {}).get(sym, []):
        out.append((int(sc.parse_ts(b["t"]).timestamp()), b["o"], b["h"], b["l"], b["c"], b["v"]))
    return out


def et(ts):
    return datetime.fromtimestamp(ts, ET)


def describe(label, bars, day_open_ts):
    """Özet satırı ve ölçüler. day_open_ts: o günün NY 09:30 zamanı (epoch)."""
    if not bars:
        return f" {label}: veri yok", None
    pre = [b for b in bars if b[0] < day_open_ts]
    early = [b for b in bars if b[0] < day_open_ts - 90 * 60]          # 08:00'den önce
    pre_vol = sum(b[5] for b in pre)
    first, last = et(bars[0][0]), et(bars[-1][0])
    line = (f" {label}: {len(bars)} mum, ilk {first:%H:%M} son {last:%H:%M} NY | "
            f"seans öncesi {len(pre)} mum, hacim {int(pre_vol):,} | 08:00'den önce {len(early)} mum")
    return line, {"first": first, "pre_vol": pre_vol, "early": len(early), "last_ts": bars[-1][0]}


def main():
    sc.check_settings()
    now = datetime.now(timezone.utc)
    out = [f"🔬 Yahoo seans öncesi veri denemesi (çalışma: {now.astimezone(ET):%H:%M} NY / {now.astimezone(ZoneInfo('Europe/Istanbul')):%H:%M} TR)"]
    verdict = {"yahoo_early": 0, "yahoo_ok": 0, "lags": []}
    for sym in SYMS:
        ybars, err = yahoo_bars(sym)
        out.append("")
        if err and not ybars:
            out.append(f"{sym}: Yahoo hata verdi ({err})")
            continue
        day = et(ybars[-1][0]).date()
        open_ts = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET).timestamp())
        out.append(f"{sym} ({day.isoformat()})")
        yline, ym = describe("Yahoo", ybars, open_ts)
        out.append(yline)
        try:
            aline, am = describe("Alpaca IEX", alpaca_bars(sym, day), open_ts)
        except Exception as e:
            aline, am = f" Alpaca IEX: hata ({str(e)[:80]})", None
        out.append(aline)
        if ym:
            verdict["yahoo_ok"] += 1
            if ym["early"] > 0:
                verdict["yahoo_early"] += 1
            if am and am["pre_vol"] > 0:
                out.append(f" Yahoo seans öncesi hacmi, IEX'in {ym['pre_vol'] / am['pre_vol']:.0f} katı")
            # gecikme: şimdi ile son mumun başlangıcı arası (dakika), sadece mum bugünün ise ve seans içindeyse anlamlı
            if et(ym["last_ts"]).date() == now.astimezone(ET).date():
                lag = (now.timestamp() - ym["last_ts"]) / 60
                verdict["lags"].append(lag)
                out.append(f" Son Yahoo mumunun başlangıcı {lag:.0f} dk önce (5 dk'lık mum için 0-5 dk normal)")
        time.sleep(1)

    out.append("")
    out.append("Sonuç:")
    if verdict["yahoo_ok"] == 0:
        out.append("- Yahoo'dan hiç veri alınamadı. Muhtemelen GitHub sunucularını engelliyor, bu yol çalışmaz.")
    else:
        out.append(f"- Yahoo {verdict['yahoo_ok']} hissede veri verdi, {verdict['yahoo_early']} tanesinde 08:00 NY'den önceki mumlar var.")
        if verdict["lags"]:
            out.append(f"- Ölçülen gecikme: ortalama {sum(verdict['lags']) / len(verdict['lags']):.0f} dk (bu çalışma anında).")
    out.append("- Hacim ve fiyatın doğruluğunu ayrıca kontrol etmek gerekir (Webull/TradingView ile karşılaştır).")
    text = "\n".join(out)
    print(text)
    sc.send_telegram(text)


if __name__ == "__main__":
    main()
