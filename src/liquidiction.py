import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


@dataclass
class Trader:
    address: str
    rank: int
    pnl: float
    trades: int
    volume: float
    # Per-leader size multiplier set by `discover_leaders` based on quality
    # metrics + operator overrides. Default 1.0 = same proportional sizing as
    # before. Higher = trust this leader more, give them a larger mirror slice
    # of the same leader notional.
    weight: float = 1.0


class LiquidictionClient:
    def __init__(self, base_url: str = "https://liquidiction.xyz", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def leaderboard_page(self, period: str, offset: int, limit: int = 50) -> dict:
        params: dict[str, str | int] = {"period": period, "offset": offset, "limit": limit}
        r = requests.get(
            f"{self.base_url}/api/leaderboard",
            params=params,
            timeout=self.timeout,
        )
        r.raise_for_status()
        result = r.json()
        return result if isinstance(result, dict) else {}

    def top_traders(self, period: str, n: int) -> list[Trader]:
        traders: list[Trader] = []
        offset = 0
        page_size = 50
        while len(traders) < n:
            page = self.leaderboard_page(period, offset, limit=page_size)
            rows = page.get("traders", []) or []
            if not rows:
                break
            for row in rows:
                traders.append(
                    Trader(
                        address=row["address"].lower(),
                        rank=row.get("rank", -1),
                        pnl=float(row.get("pnl", 0)),
                        trades=int(row.get("trades", 0)),
                        volume=float(row.get("volume", 0)),
                    )
                )
                if len(traders) >= n:
                    break
            offset += len(rows)
            if offset >= page.get("totalTraders", 0):
                break
        return traders[:n]
