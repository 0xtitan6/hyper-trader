"""Per-coin thesis cache — a structured "BULL / BEAR / NEUTRAL" stance with
confidence and evidence, used to filter or amplify leader signals.

This is the scaffolding for a Dexter-style thesis layer. The cache itself is
just storage + lifecycle management; the GENERATOR (which decides each coin's
stance using funding, fear/greed, cross-leader concordance, etc.) ships in a
follow-up PR. The MirrorTrader hook (which consults the cache to veto or
amplify a leader signal) likewise ships separately so we can validate the
generator's outputs against live signals before letting it gate trades.

Design:
  - Each coin has at most ONE active thesis at a time (PK on coin)
  - Theses have a TTL (`expires_at`) — stale entries are filtered out by
    `get()` automatically; `prune_expired()` deletes them
  - `evidence` is an arbitrary JSON blob describing inputs used (funding
    rate, fear/greed reading, leader-converge count, etc.) — useful for
    post-hoc analysis ("why did we go BEAR on PURR at 03:14?")
  - `source` distinguishes manual operator entries from automated ones
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

STANCE_BULL = "bull"
STANCE_BEAR = "bear"
STANCE_NEUTRAL = "neutral"
VALID_STANCES = {STANCE_BULL, STANCE_BEAR, STANCE_NEUTRAL}

SOURCE_MANUAL = "manual"
SOURCE_RULE_BASED = "rule_based"
SOURCE_LLM = "llm"
SOURCE_CROSS_LEADER = "cross_leader"


@dataclass(frozen=True)
class CoinThesis:
    """A single coin's thesis snapshot."""

    coin: str
    stance: str            # bull | bear | neutral
    confidence: float      # 0.0 to 1.0
    generated_at: int      # unix epoch seconds
    expires_at: int        # unix epoch seconds — get() filters past expiry
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: int | None = None) -> bool:
        return (now or int(time.time())) >= self.expires_at

    def ttl_remaining_s(self, now: int | None = None) -> int:
        return max(0, self.expires_at - (now or int(time.time())))


class ThesisCache:
    """SQLite-backed thesis store, mediated through the existing `State` lock.

    Owns NO sqlite connection of its own — uses the State's `_connect()`
    context manager so all writes are serialized via the same RLock. This
    avoids the lock-ordering bugs that come with parallel sqlite connections.
    """

    def __init__(self, state):
        # Lazy import to avoid a circular dep if state ever imports thesis
        self._state = state

    def set(
        self,
        coin: str,
        stance: str,
        confidence: float,
        ttl_s: int,
        source: str,
        evidence: dict[str, Any] | None = None,
    ) -> CoinThesis:
        """Insert or overwrite the thesis for one coin. Returns the persisted
        record so callers can confirm.

        Raises ValueError on invalid stance, confidence out of [0,1], or
        non-positive TTL — these are programmer errors, fail loudly.
        """
        if stance not in VALID_STANCES:
            raise ValueError(f"invalid stance {stance!r}, expected one of {sorted(VALID_STANCES)}")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {confidence!r}")
        if ttl_s <= 0:
            raise ValueError(f"ttl_s must be > 0, got {ttl_s!r}")
        if not coin:
            raise ValueError("coin must be non-empty")

        now = int(time.time())
        ev_json = json.dumps(evidence or {}, separators=(",", ":"))
        with self._state._lock, self._state._connect() as conn:
            conn.execute(
                "INSERT INTO coin_thesis(coin, stance, confidence, generated_at, expires_at, source, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(coin) DO UPDATE SET "
                "  stance=excluded.stance, confidence=excluded.confidence, "
                "  generated_at=excluded.generated_at, expires_at=excluded.expires_at, "
                "  source=excluded.source, evidence=excluded.evidence",
                (coin, stance, float(confidence), now, now + ttl_s, source, ev_json),
            )
        return CoinThesis(
            coin=coin,
            stance=stance,
            confidence=float(confidence),
            generated_at=now,
            expires_at=now + ttl_s,
            source=source,
            evidence=evidence or {},
        )

    def get(self, coin: str, *, include_expired: bool = False) -> CoinThesis | None:
        """Return current thesis for coin, or None if missing/expired.

        `include_expired=True` returns the row even past expiry — useful for
        the CLI / debugging, NOT for live trading decisions."""
        with self._state._lock, self._state._connect() as conn:
            row = conn.execute(
                "SELECT coin, stance, confidence, generated_at, expires_at, source, evidence "
                "FROM coin_thesis WHERE coin = ?",
                (coin,),
            ).fetchone()
        if row is None:
            return None
        t = _row_to_thesis(row)
        if not include_expired and t.is_expired():
            return None
        return t

    def clear(self, coin: str) -> bool:
        """Remove the thesis for one coin. Returns True if a row was deleted."""
        with self._state._lock, self._state._connect() as conn:
            cur = conn.execute("DELETE FROM coin_thesis WHERE coin = ?", (coin,))
            return cur.rowcount > 0

    def all_active(self) -> list[CoinThesis]:
        """Return all non-expired theses, freshest first."""
        now = int(time.time())
        with self._state._lock, self._state._connect() as conn:
            rows = conn.execute(
                "SELECT coin, stance, confidence, generated_at, expires_at, source, evidence "
                "FROM coin_thesis WHERE expires_at > ? ORDER BY generated_at DESC",
                (now,),
            ).fetchall()
        return [_row_to_thesis(r) for r in rows]

    def prune_expired(self) -> int:
        """Delete all expired theses. Returns the number removed.
        Cheap — call from the main loop periodically (e.g. once a minute)."""
        now = int(time.time())
        with self._state._lock, self._state._connect() as conn:
            cur = conn.execute("DELETE FROM coin_thesis WHERE expires_at <= ?", (now,))
            return cur.rowcount


def _row_to_thesis(row) -> CoinThesis:
    try:
        evidence = json.loads(row["evidence"]) if row["evidence"] else {}
    except (TypeError, ValueError):
        evidence = {}
    return CoinThesis(
        coin=row["coin"],
        stance=row["stance"],
        confidence=float(row["confidence"]),
        generated_at=int(row["generated_at"]),
        expires_at=int(row["expires_at"]),
        source=row["source"],
        evidence=evidence,
    )
