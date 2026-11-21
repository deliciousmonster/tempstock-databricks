"""Rewrite the five price fixtures with seeded synthetic values.

Dates, timestamps and document shape stay as recorded; every price and volume is a random walk
from a fixed seed, so no fixture holds a real price row. Run: python tests/fixtures/synthetic_prices.py
"""

import json
import random
from pathlib import Path

HERE = Path(__file__).parent


def walk(n, start, seed):
    rnd = random.Random(seed)
    out, px = [], start
    for _ in range(n):
        o = px
        c = round(o * (1 + rnd.uniform(-0.012, 0.012)), 2)
        hi = round(max(o, c) * (1 + rnd.uniform(0, 0.006)), 2)
        lo = round(min(o, c) * (1 - rnd.uniform(0, 0.006)), 2)
        out.append((round(o, 2), hi, lo, c, rnd.randint(10_000_000, 40_000_000)))
        px = c
    return out


def av(name, start, seed):
    p = HERE / name
    doc = json.loads(p.read_text())
    ts = doc["Time Series (Daily)"]
    days = sorted(ts)  # oldest first
    for d, (o, h, lo, c, v) in zip(days, walk(len(days), start, seed)):
        ts[d] = {
            "1. open": f"{o:.4f}",
            "2. high": f"{h:.4f}",
            "3. low": f"{lo:.4f}",
            "4. close": f"{c:.4f}",
            "5. volume": str(v),
        }
    p.write_text(json.dumps(doc, indent=4) + "\n")
    return {d: ts[d] for d in days}


def yahoo_series(doc, rows):
    r = doc["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    for i, k in enumerate(("open", "high", "low", "close", "volume")):
        q[k] = [row[i] for row in rows]
    r["indicators"]["adjclose"][0]["adjclose"] = [row[3] for row in rows]
    return r


def main():
    wmt = av("av-wmt-compact.json", 100.0, 1)
    av("av-spy-compact.json", 300.0, 2)

    # recent Yahoo bars: the dates Alpha Vantage shares, with the same closes
    p = HERE / "yahoo-wmt-recent.json"
    doc = json.loads(p.read_text())
    r = doc["chart"]["result"][0]
    from datetime import UTC, datetime

    days = [datetime.fromtimestamp(t, UTC).date().isoformat() for t in r["timestamp"]]
    rows = []
    for d in days:
        b = wmt.get(d)
        rows.append(
            (float(b["1. open"]), float(b["2. high"]), float(b["3. low"]), float(b["4. close"]), int(b["5. volume"]))
            if b
            else walk(1, 100.0, hash(d) & 0xFFFF)[0]
        )
    r = yahoo_series(doc, rows)
    last = rows[-1]
    r["meta"].update(
        regularMarketPrice=last[3], fulldayPrice=last[3], regularMarketDayHigh=last[1],
        regularMarketDayLow=last[2], regularMarketVolume=last[4], fiftyTwoWeekHigh=round(last[3] * 1.1, 2),
        fiftyTwoWeekLow=round(last[3] * 0.8, 2), regularMarketChangePercent=0.0, fulldayChange=0.0,
        fulldayChangePercent=0.0, chartPreviousClose=rows[0][0], longName="Example Corp.", shortName="Example Corp.",
    )
    p.write_text(json.dumps(doc, separators=(",", ":")) + "\n")

    p = HERE / "yahoo-wmt-daily.json"
    doc = json.loads(p.read_text())
    ts = doc["chart"]["result"][0]["timestamp"]
    cuts = [0] + [i for i in range(1, len(ts)) if ts[i] - ts[i - 1] > 30 * 86400] + [len(ts)]
    rows = []
    for k, (a, b) in enumerate(zip(cuts, cuts[1:])):
        rows += walk(b - a, (3.0, 20.0, 60.0)[k], 3 + k)
    r = yahoo_series(doc, rows)
    r["meta"].pop("longName", None)
    p.write_text(json.dumps(doc, indent=1) + "\n")


if __name__ == "__main__":
    main()
