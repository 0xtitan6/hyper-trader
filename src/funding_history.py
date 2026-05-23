"""Periodic poller for realized funding payments on perp positions.

Distinct from `src/funding.py` (which caches forward-looking funding RATES
for sizing decisions). This module records ACTUAL funding payments — the
hourly USDC delta per open position — into state.db so we can close the
books gap. Our `own_fills` table captures fill PnL but not funding accrual.

At our current scale ($600 account, multi-day shorts) funding is a small
income stream (~$0.03-0.50/day depending on rates). At Stage 3+ scale this
becomes material (hundreds/year). Worth instrumenting now so the data is
ready when the magnitude matters.

Design:
  - `poll(info, address)` fetches new funding events since `last_recorded_ts`
    via HL's `userFunding` endpoint, records each into state.db.
  - Caller invokes from main loop on a 1-hour cadence (matches HL's funding
    accrual cycle).
  - Dedupe via state.db's (time_ms, coin) primary key — safe to re-poll.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .protocols import InfoProto
from .state import State

log = logging.getLogger(__name__)


class FundingHistory:
    """Polls HL's userFunding endpoint and persists events to state.db.

    Maintains an internal cursor so each poll only fetches events since the
    last recorded event timestamp. On first run (empty DB), backfills
    `initial_lookback_days` of history.
    """

    def __init__(self, state: State, initial_lookback_days: int = 30):
        self.state = state
        self.initial_lookback_days = initial_lookback_days

    def poll(self, info: InfoProto, address: str) -> int:
        """Fetch new funding events for `address`, record any not already
        seen. Returns number of newly-inserted events.

        Tolerant of HL transient errors — logs and returns 0 instead of raising.
        """
        # Cursor: 1 ms after the most recent recorded event, or
        # `initial_lookback_days` ago on cold start.
        last_ms = self.state.last_funding_event_ms()
        if last_ms is None:
            since_ms = int((time.time() - self.initial_lookback_days * 86400) * 1000)
        else:
            since_ms = last_ms + 1

        try:
            events: Any = info.post(
                "/info",
                {
                    "type": "userFunding",
                    "user": address.lower(),
                    "startTime": since_ms,
                },
            )
        except Exception:
            log.exception("FundingHistory: userFunding fetch failed for %s", address[:10])
            return 0

        if not isinstance(events, list):
            log.warning("FundingHistory: unexpected response shape (not a list)")
            return 0

        inserted = 0
        for e in events:
            if not isinstance(e, dict):
                continue
            d = e.get("delta", {})
            if not isinstance(d, dict) or d.get("type") != "funding":
                continue
            try:
                time_ms = int(e.get("time", 0))
                coin = str(d.get("coin", ""))
                usdc = float(d.get("usdc", 0))
                szi = float(d.get("szi", 0))
                rate = float(d.get("fundingRate", 0))
            except (TypeError, ValueError):
                log.warning("FundingHistory: malformed event %s", e)
                continue
            if not coin or time_ms <= 0:
                continue
            if self.state.record_funding_event(time_ms, coin, usdc, szi, rate):
                inserted += 1
        if inserted:
            log.info(
                "FundingHistory: recorded %d new funding events (cursor was %d)",
                inserted, since_ms,
            )
        return inserted
