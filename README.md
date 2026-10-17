# Weekend Oracle

Hyperliquid's builder perps (HIP-3) on shares, indices, gold, oil and currencies trade all weekend
while the real markets are shut. By Sunday evening each perp has moved, and that move is a guess at
how the real market will reopen. Weekend Oracle scores every one of those guesses against what
actually happened and publishes a one-page scoreboard.

**Live page:** <https://nobones1.github.io/weekend-oracle/>. It updates on Saturday, on Sunday
evening and on Monday afternoon (UTC).

Part of the HyperTrader10 family (HIP3 Lens, HyperTrader10). The list of what each perp tracks
comes from HIP3 Lens.

## Run it locally

```bash
python3 oracle.py            # fetch, score, write out/
python3 oracle.py --offline  # rebuild from cache/ only
python3 -m unittest discover -s tests -v
```

Open `out/index.html`. Python 3.10+ standard library only. Node is needed only for
`tools/export_markets.mjs`, which refreshes `markets.json` from HIP3 Lens's mapping.

**Being polite to Hyperliquid.**
- **The limit:** Hyperliquid allows 1,200 request-weight a minute per internet address, and other
  tools on the same address share it.
- **Run locally:** the Oracle uses at most 240 a minute (`HL_BUDGET_PER_MIN`).
- **The scheduled GitHub job:** it has a machine to itself, so it sets `WO_HL_BUDGET=480`, still
  under half the limit.
- **On a "slow down" reply (HTTP 429):** the Oracle waits a minute and halves its budget for the
  rest of the run.

**Timings, measured on 17 Oct 2026 at a cap of 240:**
- **First run:** downloads seven months of hourly prices for about 220 markets, which took 94 minutes.
- **Later runs:** fetch only new hours for live markets, which took 28 minutes.

## Files

| Path | What it is |
|---|---|
| `oracle.py` | Downloads, finds the weekends, scores, and writes `out/` |
| `page.html` | The page template; `oracle.py` puts the data inside it |
| `markets.json` | Perp base name → real-world symbol, kind and exchange (from HIP3 Lens) |
| `markets_extra.json` | Markets not yet in HIP3 Lens's mapping; HIP3 Lens wins where both list one |
| `tools/cache_guard.py` | Refuses to save a price history that lost anything |
| `tools/health.py` | Fails the run if the weekend that should be scored isn't |
| `tools/publish.sh` | Copies the public files from HyperTrader10 to this repo |
| `.github/workflows/oracle.yml` | The schedule: restore history, score, check, save, publish |
| `tests/` | No peeking, clock changes, holidays, scoring, price-match check, rate limit, safety nets |
| `cache/` | Downloaded prices (not committed). On GitHub it lives on the `cache` branch with the previous copy kept beside it. **It's the only copy of weekends Hyperliquid no longer serves** |
| `out/` | `index.html`, `weekends.json`, `weekends.csv`, `status.json` |

## How a weekend is scored

1. **Real market, Friday close and reopen** (Yahoo Finance):
   - **Shares, ETFs and stock indices:** daily bars, so the official close and official open, at
     the exchange's own local times (time-zone aware). A holiday Friday or Monday falls out of the
     real trading days.
   - **Futures, FX, rates and the dollar index:** the gap of more than 30 hours in the hourly bars
     that spans a Saturday.
2. **The perp** (Hyperliquid hourly candles), read as the close of the last *finished* hour at:
   - Friday's close.
   - **Sunday 17:00 New York time:** the headline guess, read before US futures and FX reopen, or
     an hour before the reopen if that's earlier.
   - Just before the reopen ("the last word").
3. **Moves are compared as percentages**, so a perp quoted on another scale or currency is still fair.
4. **Scores:**
   - *Beat Friday's close:* the guess was closer to the reopen than "no change".
   - *Right direction:* guesses under 0.1% are "no call".
   - *Share of the jump seen coming:* 1 − (average miss ÷ average gap).
   - *Weekends won:* the guess beat Friday's close on more than half the markets that weekend.
5. **Luck check:** an exact sign test over weekends, because markets move together.
6. **Left out:**
   - Weekends with no perp trades before the Sunday reading.
   - Perps not listed by Friday's close.
   - Markets with nothing real to compare against.
   - Markets whose price doesn't keep step with their supposed reference, which means a wrong
     match or a contract roll: the ratio jumps more than 3% between weekends more than 20% of the
     time.
