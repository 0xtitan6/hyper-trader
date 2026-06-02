"""Periodic thesis generator — writes per-coin BULL/BEAR/NEUTRAL stances to
the ThesisCache using funding rate + cross-leader concordance signals.

Architecture:

  Inputs (free, already-collected data):
    - HL funding rate per perp (via FundingTracker, already polled for sizing)
    - Cross-leader position book: which of our followed leaders currently
      hold a position in each coin, and which direction

  Rules (deterministic, no LLM):
    BULL when EITHER:
      - cross_leader_concordance ≥ 2 leaders LONG in this coin, OR
      - funding_rate_apr < -10% (shorts paying heavily — contrarian bull)
    BEAR when EITHER:
      - cross_leader_concordance ≥ 2 leaders SHORT in this coin, OR
      - funding_rate_apr > +50% (longs paying heavily — overheated)
    NEUTRAL otherwise

  Confidence:
    - 0.9 when BOTH conditions agree (concordance AND funding both point same way)
    - 0.7 when concordance ≥ 2
    - 0.5 when funding extreme but no concordance
    - Source field captures which path triggered

The output is written to `ThesisCache` (`source="rule_based"`) with a TTL
matching the generator's cycle interval — stale entries auto-expire so the
hook layer never reads zombie signals.

This module does NOT consult or modify MirrorTrader's hot path. The
MirrorTrader hook that reads from the cache is a separate concern and
ships as a follow-up PR with an opt-in config flag (default OFF) so the
generator's output can be validated against live signals before going live.

Failure modes — tolerant of:
  - One leader's fetch failing (others contribute concordance)
  - Funding fetch failing entirely (degrades to concordance-only)
  - All inputs failing → log + return without writing (no stale theses)
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.error import URLError

from .pref_client import PrefClient
from .protocols import InfoProto
from .state import State
from .thesis import (
    SOURCE_RULE_BASED,
    STANCE_BEAR,
    STANCE_BULL,
    STANCE_NEUTRAL,
    CoinThesis,
    ThesisCache,
)

log = logging.getLogger(__name__)

# Thresholds — tune via config later. APR is annualized funding %.
# HL funding accrues hourly at the published rate × position notional.
FUNDING_APR_BULL_THRESHOLD = -10.0   # below this APR (shorts pay) → contrarian bull
FUNDING_APR_BEAR_THRESHOLD = 50.0    # above this APR (longs pay) → overheated bear
CONCORDANCE_THRESHOLD = 2             # ≥2 same-direction leaders triggers the signal

CONFIDENCE_BOTH_AGREE = 0.9
CONFIDENCE_CONCORDANCE_ONLY = 0.7
CONFIDENCE_FUNDING_ONLY = 0.5

# Fear/greed caching — the alternative.me source updates ~daily so polling
# faster is wasteful. Cache 30 min, refresh on stale.
FEAR_GREED_TTL_S = 1800


class ThesisGenerator:
    """Generates per-coin theses from funding + cross-leader concordance.

    Caller invokes `run_cycle()` periodically (e.g., every 10 min from main
    loop). Theses are written to the cache with TTL = `ttl_s` (default 900s,
    50% longer than cycle interval so a missed cycle doesn't leave the
    downstream filter with empty cache).
    """

    def __init__(
        self,
        info: InfoProto,
        state: State,
        cache: ThesisCache,
        leader_addresses: list[str],
        ttl_s: int = 900,
        pref_client: PrefClient | None = None,
    ):
        self.info = info
        self.state = state
        self.cache = cache
        self.leader_addresses = [a.lower() for a in leader_addresses]
        self.ttl_s = ttl_s
        # Optional PREF client for crypto sentiment. None disables enrichment
        # without affecting any other rule — the rest of the generator runs
        # unchanged. Useful for tests + degraded mode if PREF is down.
        self.pref_client = pref_client
        self._fear_greed_value: int | None = None
        self._fear_greed_classification: str | None = None
        self._fear_greed_fetched_at: float = 0.0

    def update_leaders(self, addresses: list[str]) -> None:
        """Refresh leader list — caller invokes after discover_leaders runs."""
        self.leader_addresses = [a.lower() for a in addresses]

    def run_cycle(self) -> dict[str, CoinThesis]:
        """Compute theses for all coins our leaders currently hold. Returns
        the set of theses written (for logging/inspection)."""
        leader_positions = self._fetch_leader_positions()
        if leader_positions is None:
            log.warning("thesis_generator: all leader fetches failed; skipping cycle")
            return {}

        funding_apr = self._fetch_funding_apr()  # may be empty dict on fetch failure
        fear_greed = self._refresh_fear_greed()  # cached internally, may be None
        coin_candidates = self._candidate_coins(leader_positions)

        written: dict[str, CoinThesis] = {}
        for coin in coin_candidates:
            stance, confidence, evidence = self._classify(
                coin=coin,
                leader_positions=leader_positions,
                funding_apr=funding_apr,
                fear_greed=fear_greed,
            )
            t = self.cache.set(
                coin=coin,
                stance=stance,
                confidence=confidence,
                ttl_s=self.ttl_s,
                source=SOURCE_RULE_BASED,
                evidence=evidence,
            )
            written[coin] = t

        if written:
            counts = {STANCE_BULL: 0, STANCE_BEAR: 0, STANCE_NEUTRAL: 0}
            for t in written.values():
                counts[t.stance] += 1
            log.info(
                "thesis_generator: wrote %d theses (bull=%d, bear=%d, neutral=%d)",
                len(written), counts[STANCE_BULL], counts[STANCE_BEAR], counts[STANCE_NEUTRAL],
            )
        return written

    def _candidate_coins(
        self, leader_positions: dict[str, dict[str, float]]
    ) -> set[str]:
        """Coins we generate theses for: union of every coin any followed
        leader currently holds. Filters out empty/zero positions.
        Excludes outcomes (#) and spot (@, /) — mirror only operates on perps.
        """
        coins: set[str] = set()
        for book in leader_positions.values():
            for coin, sz in book.items():
                if sz == 0:
                    continue
                if not coin:
                    continue
                if coin.startswith("#") or coin.startswith("+"):
                    continue  # outcomes
                if coin.startswith("@") or "/" in coin:
                    continue  # spot
                coins.add(coin)
        return coins

    def _classify(
        self,
        coin: str,
        leader_positions: dict[str, dict[str, float]],
        funding_apr: dict[str, float],
        fear_greed: tuple[int, str] | None = None,
    ) -> tuple[str, float, dict[str, Any]]:
        """Apply the rule set. Returns (stance, confidence, evidence_dict)."""
        # Cross-leader concordance: count signed positions in this coin
        longs = 0
        shorts = 0
        leaders_in_coin: list[str] = []
        for addr, book in leader_positions.items():
            sz = book.get(coin, 0.0)
            if sz > 0:
                longs += 1
                leaders_in_coin.append(f"{addr[:10]}:LONG")
            elif sz < 0:
                shorts += 1
                leaders_in_coin.append(f"{addr[:10]}:SHORT")

        funding = funding_apr.get(coin)

        # Decision tree
        concord_bull = longs >= CONCORDANCE_THRESHOLD and shorts < CONCORDANCE_THRESHOLD
        concord_bear = shorts >= CONCORDANCE_THRESHOLD and longs < CONCORDANCE_THRESHOLD
        funding_bull = funding is not None and funding < FUNDING_APR_BULL_THRESHOLD
        funding_bear = funding is not None and funding > FUNDING_APR_BEAR_THRESHOLD

        evidence: dict[str, Any] = {
            "longs": longs,
            "shorts": shorts,
            "leaders": leaders_in_coin,
        }
        if funding is not None:
            evidence["funding_apr"] = round(funding, 4)
        # Fear/greed enrichment — same value across all coins per cycle
        # (it's a market-wide sentiment index, not per-asset). Captured as
        # evidence ONLY in v1; rules don't consume it yet. After live
        # observation we'll decide how to incorporate (likely: scale
        # confidence on weak signals during extreme regimes).
        if fear_greed is not None:
            evidence["fear_greed"] = fear_greed[0]
            evidence["fear_greed_classification"] = fear_greed[1]

        if concord_bull and funding_bull:
            evidence["rule"] = "concordance_bull + funding_bull"
            return STANCE_BULL, CONFIDENCE_BOTH_AGREE, evidence
        if concord_bear and funding_bear:
            evidence["rule"] = "concordance_bear + funding_bear"
            return STANCE_BEAR, CONFIDENCE_BOTH_AGREE, evidence
        if concord_bull:
            evidence["rule"] = "concordance_bull"
            return STANCE_BULL, CONFIDENCE_CONCORDANCE_ONLY, evidence
        if concord_bear:
            evidence["rule"] = "concordance_bear"
            return STANCE_BEAR, CONFIDENCE_CONCORDANCE_ONLY, evidence
        if funding_bull:
            evidence["rule"] = "funding_bull"
            return STANCE_BULL, CONFIDENCE_FUNDING_ONLY, evidence
        if funding_bear:
            evidence["rule"] = "funding_bear"
            return STANCE_BEAR, CONFIDENCE_FUNDING_ONLY, evidence
        evidence["rule"] = "neutral_default"
        return STANCE_NEUTRAL, 0.0, evidence

    def _fetch_leader_positions(
        self,
    ) -> dict[str, dict[str, float]] | None:
        """Fetch each followed leader's perp positions. Returns address →
        {coin: szi}. Missing leaders treated as no-positions (won't contribute
        to concordance). Returns None only if ALL fetches fail (skip cycle)."""
        out: dict[str, dict[str, float]] = {}
        any_success = False
        for addr in self.leader_addresses:
            try:
                us = self.info.user_state(addr) or {}
            except (URLError, OSError, ValueError, KeyError):
                log.warning("thesis_generator: user_state fetch failed for %s", addr[:10])
                continue
            except Exception:
                log.exception("thesis_generator: unexpected fetch error for %s", addr[:10])
                continue
            book: dict[str, float] = {}
            for ap in us.get("assetPositions", []) or []:
                pos = ap.get("position") if isinstance(ap, dict) else None
                if not isinstance(pos, dict):
                    continue
                coin = pos.get("coin")
                if not coin:
                    continue
                try:
                    sz = float(pos.get("szi", 0))
                except (TypeError, ValueError):
                    continue
                if sz != 0:
                    book[coin] = sz
            out[addr] = book
            any_success = True
        return out if any_success else None

    def _refresh_fear_greed(self) -> tuple[int, str] | None:
        """Return cached (value, classification) tuple, refreshing from PREF
        if cache is older than FEAR_GREED_TTL_S. Returns None if PREF client
        is not configured, or PREF call fails (graceful degrade — generator
        runs without sentiment).

        Decoupling poll cadence from `run_cycle()` cadence: at 30 min cache
        + 10 min thesis cycle, we make ~48 PREF calls/day instead of 144.
        The source (alternative.me) updates ~1×/day so anything faster is
        wasteful.
        """
        if self.pref_client is None:
            return None
        age = time.time() - self._fear_greed_fetched_at
        if age < FEAR_GREED_TTL_S and self._fear_greed_value is not None:
            return self._fear_greed_value, self._fear_greed_classification or ""
        # Stale or unset — refresh
        result = self.pref_client.get_fear_greed_index()
        if result is None:
            log.info("thesis_generator: fear_greed refresh failed; using cached if any")
            if self._fear_greed_value is not None:
                return self._fear_greed_value, self._fear_greed_classification or ""
            return None
        self._fear_greed_value = result[0]
        self._fear_greed_classification = result[1]
        self._fear_greed_fetched_at = time.time()
        log.info(
            "thesis_generator: fear_greed refreshed value=%d classification=%s",
            result[0], result[1],
        )
        return result

    def _fetch_funding_apr(self) -> dict[str, float]:
        """Fetch current funding rates for all perps, convert hourly rate to APR%.
        Returns {coin: apr_percent}. Empty dict on fetch failure (degrades to
        concordance-only signal — doesn't block the cycle)."""
        try:
            ctxs = self.info.meta_and_asset_ctxs() or []
        except Exception:
            log.exception("thesis_generator: meta_and_asset_ctxs fetch failed; degrading to concordance-only")
            return {}
        if not isinstance(ctxs, list) or len(ctxs) < 2:
            return {}
        meta = ctxs[0]
        asset_ctxs = ctxs[1]
        if not isinstance(meta, dict) or not isinstance(asset_ctxs, list):
            return {}
        universe = meta.get("universe", []) or []
        out: dict[str, float] = {}
        for i, asset_info in enumerate(universe):
            if i >= len(asset_ctxs):
                break
            ctx = asset_ctxs[i]
            if not isinstance(asset_info, dict) or not isinstance(ctx, dict):
                continue
            coin = asset_info.get("name")
            if not coin:
                continue
            try:
                hourly = float(ctx.get("funding", 0))
            except (TypeError, ValueError):
                continue
            # APR = hourly rate × 24 × 365 × 100 (as percent)
            apr_pct = hourly * 24 * 365 * 100
            out[coin] = apr_pct
        return out
