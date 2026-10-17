#!/usr/bin/env python3
"""Did the run actually score the weekend it should have?

Every weekend whose Monday 15:00 UTC has passed should be in out/weekends.json.
Exits 1 (so the GitHub job fails and GitHub emails the owner) when the newest
scored weekend is older than that, or when nothing was scored at all.
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expected_weekend(now: dt.datetime) -> dt.date:
    """The latest Saturday whose following Monday 15:00 UTC is in the past."""
    d = now.date()
    while True:
        if d.weekday() == 5 and now >= dt.datetime(d.year, d.month, d.day, 15, tzinfo=dt.timezone.utc) + dt.timedelta(days=2):
            return d
        d -= dt.timedelta(days=1)


def problems(data: dict, now: dt.datetime) -> list[str]:
    out = []
    n = (data.get("overall") or {}).get("n", 0)
    if not n:
        out.append("no market-weekends were scored")
    last = (data.get("coverage") or {}).get("last_weekend")
    want = expected_weekend(now).isoformat()
    if not last or last < want:
        out.append(f"newest scored weekend is {last}, expected {want}")
    rl = (data.get("settings") or {}).get("rate_limited", 0)
    if rl:
        out.append(f"Hyperliquid asked us to slow down {rl} time(s) (it still finished)")
    return out


if __name__ == "__main__":
    data = json.load(open(os.path.join(HERE, "out", "weekends.json")))
    now = dt.datetime.now(dt.timezone.utc)
    p = problems(data, now)
    status = {"checked": now.isoformat(timespec="seconds"), "ok": not p, "problems": p,
              "last_weekend": data["coverage"]["last_weekend"], "market_weekends": data["overall"].get("n", 0)}
    json.dump(status, open(os.path.join(HERE, "out", "status.json"), "w"), indent=1)
    print(json.dumps(status, indent=1))
    sys.exit(1 if p else 0)
