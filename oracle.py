#!/usr/bin/env python3
"""Weekend Oracle: did Hyperliquid's weekend price call Monday's open?

Hyperliquid's builder-deployed (HIP-3) perps on shares, indices, gold and oil
trade all weekend while the real markets are shut. At any moment over the
weekend, the perp's move since Friday's close is a guess at how the real market
will reopen. This script scores that guess against what actually happened, for
every weekend Hyperliquid still has hourly prices for (about seven months).

Standard library only. Nothing here touches the rest of HyperTrader10: it reads
markets.json (exported from HIP3 Lens's mapping by tools/export_markets.mjs) and
the public Hyperliquid and Yahoo APIs, caches downloads in cache/, and writes
out/weekends.json, out/weekends.csv and out/index.html.

    python3 oracle.py            # refresh data, score, build the page
    python3 oracle.py --offline  # rebuild from cache only (no network)
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "out")
UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")
HOUR = 3_600_000
DAY = 24 * HOUR

HL_URL = "https://api.hyperliquid.xyz/info"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = {"User-Agent": "Mozilla/5.0 (weekend-oracle; HyperTrader10)"}

# Hyperliquid's limit is 1,200 request-weight a minute per IP address, shared
# by every other tool on the same address that talks to Hyperliquid (wallet
# trackers, dashboards, scheduled refreshes). The Oracle takes
# at most a fifth of it, and backs off further the moment Hyperliquid says
# "slow down" (HTTP 429). A full history download costs ~104 weight per market,
# a weekly top-up ~23, so the first run is slow on purpose.
HL_BUDGET_PER_MIN = 240
# On GitHub's runner nothing else shares the address, so the scheduled job may
# set WO_HL_BUDGET higher (it uses 480: still under half the limit).
try:
    HL_BUDGET_PER_MIN = max(60, min(600, int(os.environ.get("WO_HL_BUDGET", HL_BUDGET_PER_MIN))))
except ValueError:
    pass
# A delisted market's history never changes, so it is fetched once and then
# read from the cache only.
DELISTED_REFETCH = False

# A guess smaller than this is "no call": the perp barely moved.
NO_CALL = 0.001  # 0.1%
# A reopen this big counts as a "big gap" in the summaries.
BIG_GAP = 0.01  # 1%

# Exchange-listed references are scored on daily bars (official open and
# close). Local session times by the exchange's own time zone, which Yahoo
# reports, so DST is handled by zoneinfo rather than by hand.
SESSIONS = {
    "America/New_York": ((9, 30), (16, 0)),
    "Asia/Seoul": ((9, 0), (15, 30)),
    "Asia/Tokyo": ((9, 0), (15, 30)),
    "Asia/Hong_Kong": ((9, 30), (16, 0)),
    "Asia/Shanghai": ((9, 30), (15, 0)),
    "Asia/Kolkata": ((9, 15), (15, 30)),
}
# A perp whose Friday price drifts away from its supposed reference by more
# than this, on more than MISMATCH_SHARE of weekends, is probably mapped to the
# wrong thing (or re-based). It is left out rather than allowed to pollute the score.
MISMATCH_TOL = 0.03
MISMATCH_SHARE = 0.2
# References that trade nearly round the clock (futures, FX, the dollar index):
# their daily bars have odd boundaries, so the weekend is found from hourly bars.
HOURLY_KINDS = {"commodity", "fx", "rate"}
HOURLY_SYMBOLS = {"DX-Y.NYB"}
SKIP_KINDS = {"private", "crypto", "crypto-index"}

GROUPS = [
    ("us_shares", "US shares"),
    ("us_funds", "US indices and funds"),
    ("commodities", "Gold, oil and other commodities"),
    ("asia", "Korea, Japan, Hong Kong and China"),
    ("fx", "Currencies and rates"),
]


# --------------------------------------------------------------------------
# Network, with a disk cache
# --------------------------------------------------------------------------

RATE_LIMITED = {"count": 0}


def _get_json(req: urllib.request.Request, tries: int = 5) -> object:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                # Someone on this IP (maybe another app) is near the limit:
                # wait a full minute or more so they get their share back.
                RATE_LIMITED["count"] += 1
                wait = 60 * (attempt + 1)
                print(f"  Hyperliquid said slow down (429); waiting {wait}s", file=sys.stderr)
            else:
                wait = 3 * (attempt + 1)
            if e.code == 404:
                raise
        except Exception as e:  # timeouts, resets
            last = e
            wait = 3 * (attempt + 1)
        time.sleep(wait)
    raise RuntimeError(f"gave up after {tries} tries: {last}")


def hl(body: dict) -> object:
    time.sleep(0.25)  # never back-to-back, even when the budget allows it
    req = urllib.request.Request(HL_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **UA})
    return _get_json(req)


def _cache_path(*parts: str) -> str:
    p = os.path.join(CACHE, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def hip3_universe(offline: bool) -> list[dict]:
    """Every market on every builder dex, including delisted ones."""
    path = _cache_path("hl", "universe.json")
    if offline:
        return json.load(open(path))
    out = []
    dexs = hl({"type": "perpDexs"})
    for d in dexs:
        if not d:  # the first entry is the core (crypto) dex
            continue
        meta = hl({"type": "meta", "dex": d["name"]})
        for a in meta.get("universe", []):
            out.append({"coin": a["name"], "dex": d["name"], "delisted": bool(a.get("isDelisted"))})
        time.sleep(0.5)
    json.dump(out, open(path, "w"))
    return out


class Throttle:
    """Keeps the Oracle's own Hyperliquid use under HL_BUDGET_PER_MIN. A candle
    request costs 20 plus 1 per 60 candles returned. Each 429 seen halves the
    budget for the rest of the run."""

    def __init__(self, per_min: int = HL_BUDGET_PER_MIN):
        self.per_min, self.log, self.seen_429 = per_min, [], 0

    def spend(self, w: int) -> None:
        if RATE_LIMITED["count"] > self.seen_429:
            self.seen_429 = RATE_LIMITED["count"]
            self.per_min = max(60, self.per_min // 2)
        w = min(w, self.per_min)
        now = time.time()
        self.log = [(t, x) for t, x in self.log if now - t < 60]
        while self.log and sum(x for _, x in self.log) + w > self.per_min:
            time.sleep(max(0.5, 60 - (now - self.log[0][0])))
            now = time.time()
            self.log = [(t, x) for t, x in self.log if now - t < 60]
        self.log.append((now, w))


def hl_candles(coin: str, offline: bool, throttle: Throttle, delisted: bool = False) -> list[dict]:
    """Hourly candles, cached and extended incrementally. Hyperliquid keeps the
    latest 5000 of each interval, so the cache is also what preserves history
    once it ages out of the API."""
    path = _cache_path("hl", "candles", _safe(coin) + ".json")
    have: list[dict] = json.load(open(path)) if os.path.exists(path) else []
    if offline or (delisted and have and not DELISTED_REFETCH):
        return have
    now = int(time.time() * 1000)
    # Re-fetch the last two hours too: the newest candle is still forming.
    start = have[-1]["t"] - 2 * HOUR if have else now - 5000 * HOUR
    est = max(1, (now - start) // HOUR)
    throttle.spend(20 + int(est // 60) + 1)
    got = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": int(start), "endTime": now}}) or []
    merged = {c["t"]: c for c in have}
    for c in got:
        merged[c["t"]] = {"t": int(c["t"]), "o": float(c["o"]), "c": float(c["c"]), "n": int(c.get("n", 0)), "v": float(c.get("v", 0))}
    rows = [merged[k] for k in sorted(merged)]
    # Never keep the candle that is still forming as if it were final.
    rows = [r for r in rows if r["t"] + HOUR <= now]
    json.dump(rows, open(path, "w"))
    return rows


def yahoo_bars(symbol: str, interval: str, offline: bool, max_age_s: int = 3 * 3600) -> dict:
    """Yahoo chart bars as {tz, bars:[{t,o,c}]}; null bars dropped."""
    path = _cache_path("yahoo", interval, _safe(symbol) + ".json")
    if os.path.exists(path) and (offline or time.time() - os.path.getmtime(path) < max_age_s):
        return json.load(open(path))
    if offline:
        return {"tz": None, "bars": []}
    p2 = int(time.time())
    p1 = p2 - 400 * 86400 if interval == "1d" else p2 - 300 * 86400
    q = urllib.parse.urlencode({"interval": interval, "period1": p1, "period2": p2, "includePrePost": "false"})
    req = urllib.request.Request(YAHOO_URL + urllib.parse.quote(symbol) + "?" + q, headers=UA)
    try:
        j = _get_json(req, tries=5)
    except Exception as e:
        print(f"  yahoo {symbol} {interval}: {e}", file=sys.stderr)
        return json.load(open(path)) if os.path.exists(path) else {"tz": None, "bars": []}
    res = (j.get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        out = {"tz": None, "bars": []}
    else:
        qq = res["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(res["timestamp"]):
            o, c = qq["open"][i], qq["close"][i]
            if o is None or c is None or not (o > 0 and c > 0):
                continue
            bars.append({"t": int(t) * 1000, "o": float(o), "c": float(c)})
        out = {"tz": res["meta"].get("exchangeTimezoneName"), "bars": bars}
    json.dump(out, open(path, "w"))
    return out


# --------------------------------------------------------------------------
# Weekends in the reference market
# --------------------------------------------------------------------------

def _ms(d: dt.datetime) -> int:
    return int(d.timestamp() * 1000)


def _utc(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, UTC)


def saturday_between(a_ms: int, b_ms: int) -> dt.date | None:
    """The Saturday (UTC date) lying strictly between two moments, if any."""
    d = _utc(a_ms).date() + dt.timedelta(days=1)
    end = _utc(b_ms).date()
    while d < end:
        if d.weekday() == 5:
            return d
        d += dt.timedelta(days=1)
    return None


def weekends_daily(bars: list[dict], tz_name: str) -> list[dict]:
    """Weekend gaps from daily bars of an exchange-listed reference: Friday's
    (or the last session's) official close to the next official open."""
    if tz_name not in SESSIONS:
        return []
    tz = ZoneInfo(tz_name)
    (oh, om), (ch, cm) = SESSIONS[tz_name]
    days = []
    for b in bars:
        d = _utc(b["t"]).astimezone(tz).date()
        days.append((d, b))
    out = []
    for (d0, b0), (d1, b1) in zip(days, days[1:]):
        close_t = _ms(dt.datetime(d0.year, d0.month, d0.day, ch, cm, tzinfo=tz))
        open_t = _ms(dt.datetime(d1.year, d1.month, d1.day, oh, om, tzinfo=tz))
        sat = saturday_between(close_t, open_t)
        if not sat:
            continue
        out.append({"weekend": sat.isoformat(), "close_t": close_t, "close_px": b0["c"], "open_t": open_t, "open_px": b1["o"]})
    return out


def weekends_hourly(bars: list[dict]) -> list[dict]:
    """Weekend gaps from hourly bars of a near-24h reference (futures, FX):
    the last bar before a gap of more than 30 hours that spans a Saturday, and
    the first bar after it."""
    out = []
    for b0, b1 in zip(bars, bars[1:]):
        close_t = b0["t"] + HOUR  # the bar covers [t, t+1h)
        if b1["t"] - close_t < 30 * HOUR:
            continue
        sat = saturday_between(close_t, b1["t"])
        if not sat:
            continue
        out.append({"weekend": sat.isoformat(), "close_t": close_t, "close_px": b0["c"], "open_t": b1["t"], "open_px": b1["o"]})
    return out


def sunday_call_time(weekend: str, open_t: int) -> int:
    """The headline guess is read at 17:00 New York time on the Sunday: before
    US futures and FX reopen, so nothing but the weekend perp has priced it.
    If the reference reopens earlier than that, read it an hour before."""
    sat = dt.date.fromisoformat(weekend)
    sun = sat + dt.timedelta(days=1)
    t = _ms(dt.datetime(sun.year, sun.month, sun.day, 17, 0, tzinfo=ET))
    return min(t, open_t - HOUR)


# --------------------------------------------------------------------------
# The perp at a moment
# --------------------------------------------------------------------------

def price_at(candles: list[dict], t: int) -> dict | None:
    """The perp's last traded price known at moment t: the close of the latest
    hourly candle that had *finished* by t. A candle still open at t is never
    used, because its close happened after t (that would be peeking)."""
    lo, hi, best = 0, len(candles) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if candles[mid]["t"] + HOUR <= t:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None:
        return None
    c = candles[best]
    return {"px": c["c"], "at": c["t"] + HOUR, "age_h": (t - (c["t"] + HOUR)) / HOUR}


def trades_between(candles: list[dict], a: int, b: int) -> int:
    return sum(c["n"] for c in candles if c["t"] >= a and c["t"] + HOUR <= b)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def sign(x: float) -> int:
    return (x > 0) - (x < 0)


def score_event(ev: dict, candles: list[dict]) -> dict | None:
    """One market, one weekend. Returns None when the perp has no price at
    Friday's close (not listed yet, or outside the history)."""
    fri = price_at(candles, ev["close_t"])
    if not fri or fri["age_h"] > 6:
        return None
    call_t = sunday_call_time(ev["weekend"], ev["open_t"])
    if call_t <= ev["close_t"]:
        return None
    sun = price_at(candles, call_t)
    last = price_at(candles, ev["open_t"])
    if not sun or not last:
        return None
    actual = ev["open_px"] / ev["close_px"] - 1
    guess = sun["px"] / fri["px"] - 1
    guess_last = last["px"] / fri["px"] - 1
    trades = trades_between(candles, ev["close_t"], call_t)
    return {
        "weekend": ev["weekend"],
        "fri_close_t": ev["close_t"], "reopen_t": ev["open_t"], "call_t": call_t,
        "ref_close": ev["close_px"], "ref_open": ev["open_px"],
        "perp_fri": fri["px"], "perp_sun": sun["px"], "perp_last": last["px"],
        "actual": actual, "guess": guess, "guess_last": guess_last,
        "weekend_trades": trades,
        "silent": trades == 0,
    }


def direction(guess: float, actual: float) -> str:
    """'hit', 'miss' or 'no_call' (the perp moved less than NO_CALL)."""
    if abs(guess) < NO_CALL:
        return "no_call"
    return "hit" if sign(guess) == sign(actual) and actual != 0 else "miss"


def beat_friday(guess: float, actual: float) -> bool | None:
    """Was the perp's guess closer to the real reopen than simply assuming
    the market reopens at Friday's close? None for an exact tie."""
    e_guess, e_naive = abs(actual - guess), abs(actual)
    if e_guess == e_naive:
        return None
    return e_guess < e_naive


def summarise(rows: list[dict], key: str = "guess") -> dict:
    """Aggregate scores for a set of scored rows, using rows[key] as the guess."""
    rows = [r for r in rows if not r["silent"]]
    n = len(rows)
    if not n:
        return {"n": 0}
    dirs = [direction(r[key], r["actual"]) for r in rows]
    calls = [d for d in dirs if d != "no_call"]
    hits = calls.count("hit")
    beats = [b for b in (beat_friday(r[key], r["actual"]) for r in rows) if b is not None]
    err_g = [abs(r["actual"] - r[key]) for r in rows]
    err_n = [abs(r["actual"]) for r in rows]
    big = [r for r in rows if abs(r["actual"]) >= BIG_GAP]
    big_dirs = [direction(r[key], r["actual"]) for r in big]
    big_calls = [d for d in big_dirs if d != "no_call"]
    up_share = sum(1 for r in rows if r["actual"] > 0) / n
    mae_g, mae_n = statistics.fmean(err_g), statistics.fmean(err_n)
    gx, ay = [r[key] for r in rows], [r["actual"] for r in rows]
    corr = slope = None
    if n >= 3 and statistics.pstdev(gx) > 0 and statistics.pstdev(ay) > 0:
        corr = statistics.correlation(gx, ay)
        slope = statistics.linear_regression(gx, ay).slope  # real gap per 1% of perp move
    return {
        "n": n,
        "calls": len(calls), "hits": hits, "hit_rate": hits / len(calls) if calls else None,
        "beat": sum(beats), "beat_n": len(beats), "beat_rate": sum(beats) / len(beats) if beats else None,
        "mae_guess": mae_g, "mae_friday": mae_n,
        "captured": 1 - mae_g / mae_n if mae_n else None,
        "median_err_guess": statistics.median(err_g), "median_err_friday": statistics.median(err_n),
        "big_n": len(big), "big_calls": len(big_calls), "big_hits": big_calls.count("hit"),
        "big_hit_rate": big_calls.count("hit") / len(big_calls) if big_calls else None,
        "up_share": up_share,
        "guess_up_share": sum(1 for g in gx if g >= NO_CALL) / n,
        "corr": corr, "slope": slope,
    }


def sign_test_p(wins: int, n: int) -> float | None:
    """Two-sided chance of a split at least this lopsided if it were a coin
    toss. Exact binomial; fine for the few dozen weekends we have."""
    if n == 0:
        return None
    k = max(wins, n - wins)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def weekend_table(rows: list[dict]) -> list[dict]:
    """One line per weekend across all markets. Markets move together (a bad
    weekend for shares is bad for nearly all of them), so the honest sample
    size is the number of weekends, not the number of rows."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        if not r["silent"]:
            by.setdefault(r["weekend"], []).append(r)
    out = []
    for wk in sorted(by):
        s = summarise(by[wk])
        out.append({"weekend": wk, **s, "won": (s["beat_rate"] or 0) > 0.5 if s.get("beat_n") else None})
    return out


def group_of(m: dict) -> str:
    if m["kind"] in ("fx", "rate"):
        return "fx"
    if m["kind"] == "commodity":
        return "commodities"
    if m["tz"] and m["tz"] != "America/New_York":
        return "asia"
    if m["kind"] in ("index", "etf"):
        return "us_funds"
    return "us_shares"


# --------------------------------------------------------------------------
# Live: this weekend's open guesses
# --------------------------------------------------------------------------

def pending_guess(ref: dict, method: str, candles: list[dict], now_ms: int) -> dict | None:
    """If the reference has shut for the weekend and not reopened yet, the
    perp's move since the close is this weekend's guess, still open."""
    bars = ref["bars"]
    if not bars or not candles:
        return None
    last = bars[-1]
    if method == "daily":
        tz = ZoneInfo(ref["tz"])
        (_, _), (ch, cm) = SESSIONS[ref["tz"]]
        d = _utc(last["t"]).astimezone(tz).date()
        close_t = _ms(dt.datetime(d.year, d.month, d.day, ch, cm, tzinfo=tz))
    else:
        close_t = last["t"] + HOUR
    if now_ms <= close_t + HOUR:
        return None
    # Only between Friday's close and the Monday reopen: a Saturday must lie
    # between the close and now, or now must be Saturday/Sunday.
    now_d = _utc(now_ms)
    if not (saturday_between(close_t, now_ms) or now_d.weekday() in (5, 6)):
        return None
    fri = price_at(candles, close_t)
    cur = price_at(candles, now_ms)
    if not fri or not cur or fri["age_h"] > 6:
        return None
    return {"ref_close": last["c"], "fri_close_t": close_t, "perp_fri": fri["px"], "perp_now": cur["px"],
            "guess": cur["px"] / fri["px"] - 1, "as_of": cur["at"]}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_markets() -> dict:
    """HIP3 Lens's mapping, plus markets_extra.json for bases it lacks.
    HIP3 Lens wins where both have an entry."""
    base = json.load(open(os.path.join(HERE, "markets.json")))["markets"]
    extra_path = os.path.join(HERE, "markets_extra.json")
    extra = json.load(open(extra_path))["markets"] if os.path.exists(extra_path) else {}
    out = {k: {**v, "venue": None, "note": "added by Weekend Oracle", "extra": True} for k, v in extra.items()}
    out.update(base)
    return out


def price_mismatch(rows: list[dict]) -> dict | None:
    """Does the perp actually track the reference? Compare the perp's Friday
    price with the reference's Friday close, weekend after weekend. The ratio
    may sit anywhere (some perps are quoted on another scale, or in dollars
    against a reference in won or yen) and may drift slowly with the exchange
    rate. What a wrong match does is jump: so look at the change in the ratio
    from one weekend to the next. Returns details when it looks wrong."""
    rows = sorted((r for r in rows if r["ref_close"] > 0), key=lambda r: r["weekend"])
    ratios = [r["perp_fri"] / r["ref_close"] for r in rows]
    if len(ratios) < 4:
        return None
    steps = [b / a - 1 for a, b in zip(ratios, ratios[1:])]
    off = [x for x in steps if abs(x) > MISMATCH_TOL]
    if len(off) / len(steps) > MISMATCH_SHARE:
        return {"median_ratio": statistics.median(ratios), "off": len(off), "of": len(steps)}
    return None
    med = statistics.median(ratios)
    off = [x for x in ratios if abs(x / med - 1) > MISMATCH_TOL]
    if len(off) / len(ratios) > MISMATCH_SHARE:
        return {"median_ratio": med, "off": len(off), "of": len(ratios)}
    return None


def run(offline: bool = False) -> dict:
    mapping = load_markets()
    universe = hip3_universe(offline)
    throttle = Throttle()
    now_ms = int(time.time() * 1000)

    markets, skipped = [], {"unmapped": [], "no_reference": [], "mismatch": [], "no_prices": []}
    for u in universe:
        base = u["coin"].split(":")[-1].upper()
        m = mapping.get(base)
        if not m:
            skipped["unmapped"].append(u["coin"])
            continue
        if m["kind"] in SKIP_KINDS or not m["symbol"]:
            skipped["no_reference"].append(u["coin"])
            continue
        markets.append({**u, "base": base, **m})

    print(f"{len(markets)} markets with a real-world reference; "
          f"{len(skipped['unmapped'])} unmapped, {len(skipped['no_reference'])} with nothing to compare to")

    refs: dict[str, dict] = {}
    rows, pending, per_market = [], [], []
    for i, m in enumerate(markets, 1):
        sym = m["symbol"]
        method = "hourly" if (m["kind"] in HOURLY_KINDS or sym in HOURLY_SYMBOLS) else "daily"
        key = f"{sym}|{method}"
        if key not in refs:
            daily = yahoo_bars(sym, "1d", offline)
            ref = yahoo_bars(sym, "60m", offline) if method == "hourly" else daily
            ref = {**ref, "tz": ref.get("tz") or daily.get("tz")}
            evs = weekends_hourly(ref["bars"]) if method == "hourly" else weekends_daily(ref["bars"], ref["tz"] or "")
            refs[key] = {"ref": ref, "events": evs}
        ref, evs = refs[key]["ref"], refs[key]["events"]
        if not ref["bars"]:
            skipped["no_prices"].append(m["coin"])
        m["tz"], m["method"] = ref.get("tz"), method
        m["group"] = group_of(m)
        try:
            candles = hl_candles(m["coin"], offline, throttle, m["delisted"])
        except Exception as e:
            print(f"  {m['coin']}: candles failed: {e}", file=sys.stderr)
            candles = []
        mine = []
        for ev in evs:
            r = score_event(ev, candles)
            if r:
                mine.append({"coin": m["coin"], "dex": m["dex"], "base": m["base"], "name": m["name"], "symbol": sym,
                             "group": m["group"], "extra": bool(m.get("extra")), **r})
        bad = price_mismatch(mine)
        if bad:
            skipped["mismatch"].append({"coin": m["coin"], "symbol": sym, **bad})
            mine = []
        rows.extend(mine)
        scored = len(mine)
        p = pending_guess(ref, method, candles, now_ms) if not (m["delisted"] or bad) else None
        if p:
            pending.append({"coin": m["coin"], "name": m["name"], "symbol": sym, "group": m["group"], **p})
        per_market.append({"coin": m["coin"], "scored": scored})
        if i % 20 == 0:
            print(f"  {i}/{len(markets)} markets read")

    rows.sort(key=lambda r: (r["weekend"], r["coin"]))
    weekends = weekend_table(rows)
    won = sum(1 for w in weekends if w["won"])
    decided = sum(1 for w in weekends if w["won"] is not None)
    by_market: dict[str, list[dict]] = {}
    for r in rows:
        by_market.setdefault(r["coin"], []).append(r)
    result = {
        "generated_ms": now_ms,
        "settings": {"no_call": NO_CALL, "big_gap": BIG_GAP, "hl_budget_per_min": HL_BUDGET_PER_MIN, "rate_limited": RATE_LIMITED["count"]},
        "overall": summarise(rows),
        "overall_last": summarise(rows, "guess_last"),
        "weekend_record": {"won": won, "of": decided, "p": sign_test_p(won, decided)},
        "groups": [{"key": k, "label": lab, **summarise([r for r in rows if r["group"] == k])} for k, lab in GROUPS],
        "weekends": weekends,
        "markets": sorted(
            [{"coin": c, "name": rs[0]["name"], "symbol": rs[0]["symbol"], "group": rs[0]["group"], **summarise(rs)}
             for c, rs in by_market.items()],
            key=lambda x: (-(x.get("n") or 0), x["coin"])),
        "pending": sorted(pending, key=lambda p: -abs(p["guess"])),
        "rows": rows,
        "skipped": {k: sorted(v, key=lambda x: x if isinstance(x, str) else x["coin"]) for k, v in skipped.items()},
        "coverage": {"markets": len(markets), "with_rows": sum(1 for p in per_market if p["scored"]),
                     "first_weekend": rows[0]["weekend"] if rows else None, "last_weekend": rows[-1]["weekend"] if rows else None},
    }
    return result


def write_outputs(result: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    json.dump(result, open(os.path.join(OUT, "weekends.json"), "w"), indent=None, separators=(",", ":"))
    cols = ["weekend", "coin", "name", "symbol", "group", "ref_close", "ref_open", "perp_fri", "perp_sun", "perp_last",
            "actual", "guess", "guess_last", "weekend_trades", "silent"]
    with open(os.path.join(OUT, "weekends.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in result["rows"]:
            w.writerow([r[c] for c in cols])
    tpl = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()
    slim = {k: v for k, v in result.items() if k != "rows"}
    # The page only needs each scored row's market, weekend and the two moves
    # (for the scatter); everything else is in weekends.json / weekends.csv.
    slim["rows"] = [{"weekend": r["weekend"], "coin": r["coin"], "actual": round(r["actual"], 5), "guess": round(r["guess"], 5), "silent": False}
                    for r in result["rows"] if not r["silent"]]

    def trim(x):
        if isinstance(x, float):
            return round(x, 5)
        if isinstance(x, dict):
            return {k: trim(v) for k, v in x.items()}
        if isinstance(x, list):
            return [trim(v) for v in x]
        return x
    slim = trim(slim)
    data = json.dumps(slim, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("/*__DATA__*/null", data)
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(page)


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    t0 = time.time()
    result = run(offline=offline)
    write_outputs(result)
    o = result["overall"]
    print(f"\n{o.get('n', 0)} market-weekends scored across {len(result['weekends'])} weekends in {time.time() - t0:.0f}s")
    if o.get("n"):
        print(f"  Sunday guess beat Friday's close: {o['beat']}/{o['beat_n']} ({o['beat_rate']:.1%})")
        print(f"  Direction right: {o['hits']}/{o['calls']} ({o['hit_rate']:.1%}); share of the gap captured: {o['captured']:.1%}")
        wr = result["weekend_record"]
        print(f"  Weekends won: {wr['won']}/{wr['of']} (coin-toss chance {wr['p']:.3f})")
    print(f"  page: {os.path.join(OUT, 'index.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
