"""Static NSE equity instrument map (symbol -> ISIN, NSE exchange token).

A wrong identifier would trade the wrong stock, so every entry carries provenance and live trading
refuses unverified entries (see `nse_equity.json` meta and scripts/refresh_instruments.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.domain.errors import InvalidInstrument

_FILE = Path(__file__).with_name("nse_equity.json")


@dataclass(frozen=True)
class NseEquity:
    symbol: str
    isin: str
    token: str
    verified: bool


@lru_cache(maxsize=1)
def _load() -> tuple[bool, dict[str, dict[str, str]]]:
    data = json.loads(_FILE.read_text(encoding="utf-8"))
    return bool(data["meta"].get("verified")), data["instruments"]


def lookup(symbol: str, *, live_trading: bool) -> NseEquity:
    file_verified, instruments = _load()
    entry = instruments.get(symbol)
    if entry is None:
        raise InvalidInstrument(f"NSE:{symbol} is not in the instrument map (run scripts/refresh_instruments.py)")
    verified = file_verified or entry.get("source") in ("angelone-docs", "upstox-master")
    if live_trading and not verified:
        raise InvalidInstrument(
            f"instrument data for NSE:{symbol} is unverified; refusing to trade it live "
            "(run scripts/refresh_instruments.py)"
        )
    return NseEquity(symbol=symbol, isin=entry["isin"], token=entry["token"], verified=verified)
