#!/usr/bin/env python3
"""Refuse to save a price history that has lost anything.

Hyperliquid only serves the latest 5,000 hours of candles, so cache/ is the
only copy of older weekends. A run that breaks or truncates it must fail before
the broken copy replaces the good one.

    python3 tools/cache_guard.py snapshot > before.json   # before the run
    python3 tools/cache_guard.py check before.json        # after: exit 1 if anything shrank
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDLES = os.path.join(HERE, "cache", "hl", "candles")


def snapshot() -> dict:
    out = {}
    for f in sorted(glob.glob(os.path.join(CANDLES, "*.json"))):
        rows = json.load(open(f))  # raises on a truncated file
        out[os.path.basename(f)] = {"n": len(rows), "first": rows[0]["t"] if rows else None}
    return out


def check(before: dict, after: dict) -> list[str]:
    problems = []
    for name, b in before.items():
        a = after.get(name)
        if a is None:
            problems.append(f"{name}: file disappeared ({b['n']} hours)")
        elif a["n"] < b["n"]:
            problems.append(f"{name}: shrank from {b['n']} to {a['n']} hours")
        elif b["first"] is not None and (a["first"] is None or a["first"] > b["first"]):
            problems.append(f"{name}: lost its oldest hours")
    return problems


if __name__ == "__main__":
    if sys.argv[1:2] == ["snapshot"]:
        json.dump(snapshot(), sys.stdout)
    elif sys.argv[1:2] == ["check"] and len(sys.argv) == 3:
        before = json.load(open(sys.argv[2]))
        problems = check(before, snapshot())
        if problems:
            print("Price history lost data; not saving it:\n  " + "\n  ".join(problems))
            sys.exit(1)
        print(f"Price history OK: {len(before)} markets kept, none shrank.")
    else:
        print(__doc__)
        sys.exit(2)
