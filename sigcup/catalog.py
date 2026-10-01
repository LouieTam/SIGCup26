"""Market catalog: pair R/D/I legs by raceId and cache to disk.

`GET /markets/{id}/nodes` carries `contract_details` with the machine-readable
resolution spec (raceId, winnerName, resolutionType, usState). That is strictly
better than the Info-tab prose, and raceId pairs the legs of a race exactly.

One node read per market, so building the catalog costs ~237 reads (~2.5 min
under the 100 read/min budget). Cache it; markets don't change intraday.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

CATALOG_PATH = "data/catalog.json"

# contract_details keys we care about, flattened onto each row.
_DETAIL_KEYS = (
    "raceId",
    "raceName",
    "raceStage",
    "usState",
    "winnerName",
    "resolutionType",
    "officeLevel",
    "contractType",
    "electionDate",
)


def build_catalog(client, verbose: bool = True) -> list[dict]:
    markets = client.tournament_markets()
    rows: list[dict] = []
    for i, m in enumerate(markets, 1):
        exchanges = m.get("exchanges") or []
        row: dict[str, Any] = {
            "marketId": str(m["id"]),
            "title": m.get("title"),
            "status": m.get("status"),
            "settlementDate": m.get("settlementDate"),
            "isMultiOutcome": m.get("isMultiOutcome"),
            "exchangeId": str(exchanges[0]["id"]) if exchanges else None,
            "exchangeCount": len(exchanges),
        }
        try:
            root = client._get(
                f"/markets/{m['id']}/nodes", tournamentId=client.tournament_id
            )["root"]
            details = root.get("contract_details") or {}
            row["nodeContractType"] = root.get("contract_type")
            for k in _DETAIL_KEYS:
                row[k] = details.get(k)
        except Exception as e:  # keep the row; flag it for review
            row["nodeError"] = f"{type(e).__name__}: {e}"
        rows.append(row)
        if verbose and i % 25 == 0:
            print(f"  …{i}/{len(markets)} markets")
    return rows


def save_catalog(rows: list[dict], path: str = CATALOG_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(rows, f, indent=1)


def load_catalog(path: str = CATALOG_PATH) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def group_by_race(rows: list[dict]) -> dict[str, list[dict]]:
    """raceId -> legs. Rows without a raceId are skipped (flag them separately)."""
    races: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("raceId"):
            races[str(r["raceId"])].append(r)
    return dict(races)


def needs_review(rows: list[dict]) -> list[dict]:
    """Markets whose resolution is not a plain party-winner question, or whose
    node read failed. These need a human to read the wording before we quote."""
    out = []
    for r in rows:
        if r.get("nodeError") or not r.get("raceId"):
            out.append(r)
        elif r.get("resolutionType") != "Party Winner":
            out.append(r)
    return out


def select(
    rows: list[dict],
    state: str | None = None,
    race_id: str | None = None,
    market_ids: list[str] | None = None,
    title_contains: str | None = None,
) -> list[dict]:
    """Pick a focus set. Filters combine as OR when several are given."""
    if not any([state, race_id, market_ids, title_contains]):
        return [r for r in rows if r.get("exchangeId")]
    picked: dict[str, dict] = {}
    for r in rows:
        if not r.get("exchangeId"):
            continue
        hit = (
            (state and (r.get("usState") or "").upper() == state.upper())
            or (race_id and str(r.get("raceId")) == str(race_id))
            or (market_ids and r["marketId"] in set(map(str, market_ids)))
            or (
                title_contains
                and title_contains.lower() in (r.get("title") or "").lower()
            )
        )
        if hit:
            picked[r["marketId"]] = r
    return list(picked.values())
