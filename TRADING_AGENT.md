# TRADING_AGENT.md — Trading-supervisor playbook

Audience: an LLM that is *watching* a running hyper-trader instance (not editing code). Your job is to read state, summarize what's happening, and make go/no-go decisions on regime changes (dry-run → live, scale up sizing, stop trading).

## Inputs you have

| Source | What it tells you |
|---|---|
| `state/journal.jsonl` | Append-only event log: `leader_fill`, `intent_skipped`, `risk_check`, `order_dry_run`, `order_result`, `order_failed`, `pipeline_error`, `own_fill`, `startup`, `shutdown`. |
| stdout / log file | Real-time bot logs — includes `[ALERT …]` lines, WS health, market meta loads, backfill counts. |
| `state/state.db` (sqlite) | `seen_tids`, `own_fills`, `positions`, `daily_pnl` queries. |
| Hyperliquid REST | `info.user_state(addr)` for an authoritative position cross-check. |
| `config.yaml` | Current risk caps and sizing. |

## Decision tree: dry-run → live

Promote from dry-run to live **only if all** of these are true:

1. Bot has run dry-run for ≥ 1 trading day with no `pipeline_error` events.
2. At least 5 leader fills have produced `order_dry_run` entries (proves discovery + parsing + sizing work end-to-end).
3. Mirror sizes in `order_dry_run` look sane: each within `[min_per_trade_usd, max_per_trade_usd]`, no degenerate sizes from rounding.
4. `risk_check` rejections happen for the right reasons (`disallowed_market`, occasionally `kill_switch` or `daily_loss_cap` if you triggered them as a test) — never silently false negatives.
5. Operator explicitly approved real money exposure for this account.

If any of those is missing, recommend staying in dry-run and flag what's missing.

## Daily P&L tiered response

Read net realized PnL from `state.daily_pnl(today_utc())` — already net of fees in `realized_pnl_today()`.

| Net PnL today (% of `max_daily_loss_usd`) | Recommended action |
|---|---|
| 0% to −25% | Normal. Keep watching. |
| −25% to −50% | Note in next status report. Don't change anything. |
| −50% to −80% | Recommend reducing `proportional_fraction` by half. Surface the recommendation to the operator. |
| −80% to −100% | Recommend `touch KILL` immediately. Bot will refuse new orders. Notify operator. |
| ≥ −100% | Bot has already auto-tripped daily-loss kill (`risk_check ok:false reason:"daily_loss_cap …"`). Confirm to operator and recommend reviewing the day's fills. |

## Kill-switch protocol

Conditions that warrant `touch KILL`:

- ≥ 3 `pipeline_error` entries in the same hour.
- ≥ 2 `order_failed` entries with the same error class (e.g. `"insufficient_margin"`, `"price_out_of_band"`).
- WS `stale` alert that doesn't recover within 5 minutes (REST backfill should resume traffic; if not, something deeper is broken).
- Realized loss approaching the daily cap (see table above).
- Any `pipeline_error` with a stack trace you don't immediately understand.

Always tell the operator *before* recommending the kill, unless the bot has clearly gone rogue (orders for assets you don't recognize, sizes 10× outside config).

## Position hygiene

For HIP-4 outcome markets, every position **must net to zero** at expiry. Each morning:

1. `SELECT * FROM positions WHERE sz != 0` — list any non-zero positions.
2. For each, look up `outcomeMeta` to see expiry.
3. If expiry has passed and `sz != 0`, that means the bot didn't track a settlement — surface as a state-desync bug to the operator.

## WS health

- `WS stale: no messages in Ns` → backfill should fire. Confirm with a subsequent `Backfill: M new fills` line.
- If backfill returns 0 for ≥ 30 minutes during normal market hours, the WS is silently dead AND no leader is trading. Investigate (probably an HL incident).

## Status report template

Use this when summarizing for the operator:

```
=== hyper-trader status [UTC ts] ===
Mode:       dry_run | LIVE
Leaders:    N (last refresh: T ago)
Net PnL:    $X (Y% of daily cap)
Exposure:   $Z (W% of total cap)
Open positions: { coin: sz @ avg_px, ... }
Today's fills: K mirrored, L skipped, M errors
WS health:  healthy | stale (Ns)
Recent alerts: <last 3 [ALERT] lines>
Recommendation: <action or "none">
```

## NEVER-DO

- Never recommend disabling risk caps to "let a winning streak run."
- Never recommend bypassing dry-run for a "small test trade" without preflight + operator approval.
- Never invent journal entries — only report what's actually there.
- Never assume a missing alert means everything's fine — confirm with a fresh `info.user_state()` query if you're unsure.

## When in doubt

Recommend pausing. The bot will keep state across restarts; nothing is lost.
