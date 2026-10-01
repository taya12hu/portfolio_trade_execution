"""Regenerate app/brokers/instruments/nse_equity.json from official broker instrument masters.

    python scripts/refresh_instruments.py              # Upstox master + AngelOne cross-check
    python scripts/refresh_instruments.py --no-angel   # Upstox master only

Upstox's NSE master gives, per cash-equity instrument, the ISIN-based instrument_key and the NSE
exchange token. AngelOne's `symboltoken` is that same NSE token, so the AngelOne scrip master is used
to cross-check it; any disagreement aborts without writing. Downloads are a few MB (gzip/JSON).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

UPSTOX_NSE = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
ANGEL_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
OUT = Path(__file__).resolve().parents[1] / "app" / "brokers" / "instruments" / "nse_equity.json"
REQUIRED = ("segment", "instrument_type", "trading_symbol", "instrument_key", "exchange_token")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_upstox(client: httpx.Client) -> dict[str, dict[str, str]]:
    resp = client.get(UPSTOX_NSE)
    resp.raise_for_status()
    rows = json.loads(gzip.decompress(resp.content))
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("segment") != "NSE_EQ" or row.get("instrument_type") != "EQ":
            continue
        missing = [k for k in REQUIRED if not row.get(k)]
        if missing:
            fail(f"Upstox master row for {row.get('trading_symbol')} is missing {missing}; format changed?")
        prefix, _, isin = row["instrument_key"].partition("|")
        if prefix != "NSE_EQ" or len(isin) != 12 or not isin.startswith("IN"):
            fail(f"unexpected instrument_key {row['instrument_key']!r}")
        out[row["trading_symbol"]] = {"isin": isin, "token": str(row["exchange_token"]), "source": "upstox-master"}
    if len(out) < 1000:
        fail(f"only {len(out)} NSE equities parsed; refusing to write a partial map")
    return out


def cross_check_angel(client: httpx.Client, instruments: dict[str, dict[str, str]]) -> None:
    resp = client.get(ANGEL_MASTER)
    resp.raise_for_status()
    angel = {r["symbol"][:-3]: str(r["token"]) for r in resp.json()
             if r.get("exch_seg") == "NSE" and str(r.get("symbol", "")).endswith("-EQ")}
    mismatches = [(s, v["token"], angel[s]) for s, v in instruments.items() if s in angel and angel[s] != v["token"]]
    if mismatches:
        fail(f"{len(mismatches)} token mismatches between Upstox and AngelOne, e.g. {mismatches[:5]}")
    print(f"AngelOne cross-check ok: {sum(1 for s in instruments if s in angel)} symbols agree")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-angel", action="store_true", help="skip the AngelOne cross-check")
    args = parser.parse_args()
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        instruments = load_upstox(client)
        if not args.no_angel:
            cross_check_angel(client, instruments)
    data = {
        "meta": {
            "description": "NSE cash-equity identifiers (ISIN, NSE exchange token).",
            "verified": True,
            "source": UPSTOX_NSE + ("" if args.no_angel else f" (cross-checked against {ANGEL_MASTER})"),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "instruments": dict(sorted(instruments.items())),
    }
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(instruments)} instruments to {OUT}")


if __name__ == "__main__":
    main()
