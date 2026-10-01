#!/usr/bin/env python3
"""Build data/catalog.json: every tournament market + its resolution spec.

Read-only. Costs ~1 read per market (~237), so ~2.5 min under the 100/min
budget. Re-run only when the market set changes.

    python3 build_catalog.py
"""

import sys
from collections import Counter

from sigcup.catalog import build_catalog, group_by_race, needs_review, save_catalog
from sigcup.client import SuperMarketClient, load_env


def main() -> int:
    load_env()
    c = SuperMarketClient()
    print("Building catalog (one node read per market)…")
    rows = build_catalog(c)
    save_catalog(rows)
    print(f"\nWrote data/catalog.json — {len(rows)} markets")

    races = group_by_race(rows)
    sizes = Counter(len(legs) for legs in races.values())
    print(f"\n{len(races)} races, legs per race: {dict(sorted(sizes.items()))}")

    print("\nBy resolutionType:")
    for k, v in Counter(r.get("resolutionType") for r in rows).most_common():
        print(f"  {str(k):<24} {v}")
    print("\nBy officeLevel:")
    for k, v in Counter(r.get("officeLevel") for r in rows).most_common():
        print(f"  {str(k):<24} {v}")

    # Races with a third leg are the ones where an independent is live, so
    # p_R + p_D is meaningfully below 1 (HANDOFF §3.3).
    three_way = {rid: legs for rid, legs in races.items() if len(legs) > 2}
    print(f"\n{len(three_way)} races with 3+ legs (independent live):")
    for rid, legs in sorted(three_way.items(), key=lambda kv: kv[1][0]["raceName"] or ""):
        winners = ", ".join(sorted(str(l.get("winnerName")) for l in legs))
        print(f"  race {rid} {legs[0].get('raceName')}: {winners}")

    review = needs_review(rows)
    print(f"\n{len(review)} markets need human review of their wording:")
    for r in review[:40]:
        why = r.get("nodeError") or (
            "no raceId" if not r.get("raceId") else f"resolutionType={r.get('resolutionType')!r}"
        )
        print(f"  {r['marketId']:>5}  {why:<34} {(r.get('title') or '')[:58]}")
    if len(review) > 40:
        print(f"  … and {len(review) - 40} more (see data/catalog.json)")

    ri = [r for r in rows if (r.get("usState") or "").upper() == "RI"]
    print(f"\nRhode Island markets ({len(ri)}):")
    for r in sorted(ri, key=lambda r: r["marketId"]):
        print(
            f"  m={r['marketId']:>5} ex={str(r.get('exchangeId')):>5} "
            f"race={r.get('raceId')} {str(r.get('winnerName')):<18} {r.get('raceName')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
