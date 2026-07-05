# HIP-4 Maker — Go-Live Runbook

Standalone Avellaneda-Stoikov / GLFT market-maker for a **single HIP-4 outcome leg**.
Runs as its own process, on its **own HL subaccount**, isolated from the live mirror bot.

> ⚠️ Do NOT run this on the mirror bot's main account. Use a separate, USDH-funded
> subaccount and its own kill-switch file. One maker process = one outcome coin.

## 1. Prerequisites
- A separate HL **subaccount** controlled by the same wallet as `HL_PRIVATE_KEY` in `.env`.
- That subaccount funded with **USDH** (HIP-4 settles in USDH; $10 min order value).
- The target outcome leg's `--coin` (e.g. `#20`) and its **resolution/expiry** time (ISO8601).
- Repo `.venv` + a valid `config.yaml` (reuses the mirror's for API url / logging / creds).

## 2. Dry-run first (ALWAYS)
Validate quoting on live data **without sending orders**:
```
.venv/bin/python -m src.maker \
  --coin '#20' --expiry 2026-05-06T06:00:00+00:00 \
  --account-address 0xYOUR_SUBACCOUNT \
  --kill-file ./KILL.maker \
  --dry-run
```
Watch `state/` journal for `maker_quote` / `maker_toxicity` / `maker_skip` events. Confirm:
- quotes straddle mid with spread ≥ `--min-spread-bps` (default 30 bps),
- inventory skew leans quotes the right way as simulated fills accrue,
- `match_gate` skips near the resolution window,
- no tracebacks.

## 3. Go live (small)
Drop `--dry-run`. Start with the **conservative defaults** and small size:
```
.venv/bin/python -m src.maker \
  --coin '#20' --expiry 2026-05-06T06:00:00+00:00 \
  --account-address 0xYOUR_SUBACCOUNT \
  --kill-file ./KILL.maker \
  --quote-size 1.0 --max-position 20 --max-inventory-usd 5 --min-spread-bps 30
```
Launch detached (survives your shell), same pattern as the mirror engine:
```
setsid bash -c 'exec .venv/bin/python -m src.maker ... >> state/maker.log 2>&1' </dev/null &
```

Then start its watchdog (alerts on process-down / duplicate / stale log):
```
setsid bash -c 'exec .venv/bin/python scripts/maker_watchdog.py \
  --coin "#20" --log state/maker.log --interval 60' </dev/null &
```

## 4. Risk knobs (all enforced in `MakerConfig`)
| Flag / config | Default | Meaning |
|---|---|---|
| `--quote-size` | 1.0 | shares posted per side |
| `--max-position` | 20 | hard cap on net long shares |
| `--max-inventory-usd` | $5 | hard $ at risk per side |
| `--min-spread-bps` | 30 (0.30%) | spread floor (well above the ~0.75bp close fee) |
| `max_quote_px` | 0.99 | never quote above this prob |
| `match_gate_preroll_s` | 120 | stop quoting this long before kickoff |
| `match_gate_cooldown_s` | 300 | resume this long after est. end |

Adverse-selection defenses (auto): TFI (trade-flow imbalance) widen/cancel the hit side,
QI (queue imbalance), depth-evaporation pull, and a stand-down timer after a toxic pull.

## 5. Kill switch
`touch ./KILL.maker` → the maker's next loop logs `KILL switch active` and **exits**.
This is a *separate* file from the mirror bot's `./KILL`, so killing one never stops the other.

## 6. First-hour watch
- **Inventory**: should oscillate around 0, never pin at `--max-position` (if it does, spread's too tight or flow's toxic → widen `--min-spread-bps`).
- **Realized spread capture** vs fees in the journal — must be net positive.
- **`maker_skip` reasons**: occasional `standdown`/`match_gate`/`empty_book` are normal; a flood means the book's too thin to make — stop.
- **No one-sided hangs** after a `cancel_side` (was the fixed test's concern).

## 7. Stop
`touch ./KILL.maker`, confirm the process exits, then **flatten any residual inventory**
on the subaccount manually before walking away.

---
_Status: maker suite 105 green; subaccount isolation + liveness watchdog done. Paper-trade
on the subaccount first, then go live small._
