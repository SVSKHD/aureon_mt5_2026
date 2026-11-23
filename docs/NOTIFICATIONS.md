# Notification intelligence and the trade management switch

Status: **not released** (branch `claude/admiring-archimedes-mbjesh`). `VERSION` is unchanged; set it when this is
approved. Nothing here is live until it is merged into the branch the watchdog auto-updates from.

> ⚠️ **Upgrade note: automatic trailing stops stop by default.** `TRADE_MANAGEMENT_ENABLED` defaults to `false`.
> A running bot with `TSL_MODE=auto` that relies on the stepped trailing stop today will **stop moving stops** once
> this is merged and auto-updated. It keeps tracking positions and reports each step as "TSL step NOT applied —
> trade management is OFF". To keep today's behaviour, put `TRADE_MANAGEMENT_ENABLED=true` in `.env` **before**
> the merge, or run `/trade_management enable` (then Confirm) after it.

## 1. What was verified in the existing code (audit)

Every notification goes through `notifier.deliver()` (send now + retry queue) or `notifier.send()` (send now only).

| Producer | Kinds | Channel → services (standard) |
|---|---|---|
| `bot.check_ema` | signal and label cards (`CROSS`, `PRE-CROSS`, `GAP PEAKED`, `PULLBACK FAILED`, 200-crosses) | `#alerts` → Discord, Slack, ntfy |
| `bot.check_ema` | context labels (`GAP WIDENING`, `PARALLEL RUNNING`, `WIDE GAP`, `PULLBACK STARTED`, `RE-ENTRY`, narrow zone) | `#updates` → Discord only |
| `bot.check_ema` | `DIGEST` (missed while offline), replay/history warnings (`WARN`) | `#alerts` |
| `bot.send_chart` | `CHART` (`send(only=["Discord"])`, not recorded, not retried) | Discord |
| `bot` system | `BLIND`, `RECOVERED`, `SCRIPT_FAILURE`, clock corrected, state recovered (`WARN`) | `#alerts` |
| `bot` system | `START`, `ALIVE`, `PAUSE`, watchdog/updater/uploader notices (`SYSTEM_*`) | `#updates` (rollback/crash: `#alerts`) |
| `bot._check_price_alerts` | `PRICE_ALERT` (calls `send()` directly, queues failures itself) | `#alerts` |
| `positions.py` | `POSITION_OPENED/CLOSED/UNMANAGED/AGAINST_TREND`, `TSL_MOVED/SUGGEST/FAILED`, `TREND_RUNNING`, `TREND_STRENGTHENING_SL` | `#positions` → Discord only |
| `positions.max_point` | `MAX_POINT` (Close/Keep buttons) | `#alerts` |
| `weekend.py` | `WEEKEND_WARNING`, `WEEKEND_FLAT`, `WEEKEND_CARRIED` | `#alerts` / `#positions` |
| `monitor.py` | `MONITOR` (edited in place, `send()` directly) | `#updates` → Discord bot client |
| `notifier.send` | "⚠️ X webhook is failing" (plain text) | the services that did deliver |

Discord: a per-channel webhook, or the bot client (through `logs/outbox/`) for cards with buttons or edited in place.
**Assembler** receives no notifications directly: it receives `alert` records (title, kind, dedupe key,
delivered/failed) from `store.record`, uploaded only with `RECORD_SINK=api`.

Gaps found:
- No severity model: `ping` + channel was the only priority signal.
- No general rate limit: only per-feature cooldowns (failure notices, monitor edits, weekend cadence).
- No notification audit trail or per-delivery status beyond the `alert` record.
- `PRICE_ALERT`, `MONITOR` and `CHART` bypass `deliver()`.
- Webhook posts are synchronous inside the loop (in parallel, with short timeouts).
- `ARCHITECTURE.md` says "Slack and ntfy get the `#alerts` set only". True, and a retried message is also re-filtered
  that way.
- No switch for the bot's own stop moves: auto-TSL ran whenever `TSL_MODE=auto`.

## 2. Modes

`NOTIFY_MODE` in `.env` is the starting default (`standard`). `/notify_mode` changes it at run time; the choice is
persisted in `logs/state/notify.json` and survives restarts and auto-updates.

| Mode | Behaviour |
|---|---|
| `standard` (default) | Exactly the earlier delivery. `deliver()` takes the old code path; the only additions are a local audit line and secret redaction (a card is changed only if it contains a token, webhook URL or the ntfy topic). |
| `intelligent` | Severity routing, grouping, digests, rate limits, duplicate suppression, quiet hours, per-service and per-symbol thresholds, context lines. |
| `monitor` | As `intelligent`, but normal/low events of a symbol update one live Discord card per symbol (`📡 FEED`, edited in place) instead of group/digest cards. Critical and high still arrive at once as their own cards. Needs the Discord bot client; without it, events are grouped as in `intelligent`. |

If the policy itself raises, the message is delivered the standard way (`notifier.deliver`), so a policy bug can
never lose a notification. No LLM decides anything: severity and routing are the fixed rules below.

## 3. Severity (deterministic, `notify_events.SEVERITY`)

| Severity | Kinds | intelligent / monitor |
|---|---|---|
| critical | `BLIND`, `SCRIPT_FAILURE`, `TSL_FAILED`, `POSITION_UNMANAGED`, `SYSTEM_ROLLED_BACK`, `SYSTEM_CRASHED`, `WEEKEND_WARNING`, `WEEKEND_CARRIED`, `TRADE_MANAGEMENT`, and `POSITION_OPENED` without a stop-loss | at once, to **every** enabled service (also Slack/ntfy for `#positions`), with the buzz; never grouped, digested, rate-limited, quieted or suppressed as a duplicate |
| high | signals, `PRICE_ALERT`, `MAX_POINT`, position events, `TSL_SUGGEST`, `TSL_BLOCKED`, `DIGEST`, `WARN`, any kind not listed | at once; no buzz in quiet hours; beyond the rate limit → grouped |
| normal | context labels, `TREND_RUNNING`, `RECOVERED`, update notices, `WEEKEND_FLAT` | grouped per symbol for `NOTIFY_GROUP_SECONDS` (monitor: feed card) |
| low | `START`, `ALIVE`, `PAUSE` | digest every `NOTIFY_DIGEST_MINUTES` (monitor: feed card) |

Escalations: a card that pings is at least high; a card with buttons is at least high and is never held (its
buttons would be lost). Edited-in-place cards (`MONITOR`, the controls card) and charts pass through unchanged.

Nothing is dropped except an identical non-critical card inside `NOTIFY_DUPLICATE_SECONDS`. Events below a
per-symbol minimum go to the digest; they are not discarded.

## 4. Configuration (`.env`)

```
# notifications (all optional)
NOTIFY_MODE=standard                 # standard | intelligent | monitor (starting default; /notify_mode overrides)
NOTIFY_QUIET_HOURS=                  # e.g. 23:00-07:00 (IST). Critical always sent; high without the buzz
NOTIFY_DIGEST_MINUTES=30             # low-priority digest interval
NOTIFY_GROUP_SECONDS=60              # normal events of one symbol are grouped this long
NOTIFY_RATE_MAX=8                    # immediate non-critical cards per symbol ...
NOTIFY_RATE_WINDOW_SECONDS=600       # ... per this window (the rest is grouped)
NOTIFY_DUPLICATE_SECONDS=600         # identical non-critical card within this = not sent again
NOTIFY_MIN_SEVERITY_DISCORD=low      # lowest severity each service carries (critical always goes)
NOTIFY_MIN_SEVERITY_SLACK=high
NOTIFY_MIN_SEVERITY_NTFY=high
NOTIFY_DISABLED_SERVICES=            # e.g. Slack  (a critical still goes to the others; if all are off, to all)
NOTIFY_SYMBOL_PREFS=                 # e.g. XAGUSD:min=high  (below = digest)
NOTIFY_ASSEMBLER_EXPORT=0            # 1 = also write the audit as "notification" records (RECORD_SINK=api uploads them)

# trade management (the bot's own stop moves)
TRADE_MANAGEMENT_ENABLED=false       # default false. Independent of PLACE_ENABLED and TSL_MODE
TRADE_MANAGEMENT_RESTORE_ENABLED=0   # 1 = an ON value set in Discord is restored after a restart
TRADE_MANAGEMENT_USER_IDS=           # optional: only these may switch it (must also be in DISCORD_ALLOWED_USER_IDS)
```

The `NOTIFY_*` thresholds apply only in `intelligent` / `monitor` mode. A bad `NOTIFY_*` value stops the bot at
startup with a clear message (the same as `RECORD_SINK`). A malformed `TRADE_MANAGEMENT_ENABLED` does **not** stop
the bot: management stays OFF and a warning card says why.

Files: `logs/state/notify.json(.bak)`, `logs/state/trade_management.json(.bak)` (atomic, bot loop only),
`logs/notify/audit-YYYY-MM.jsonl` (one line per decision/delivery; not a record file, so it is not uploaded unless
`NOTIFY_ASSEMBLER_EXPORT=1`).

## 5. Discord commands

| Command | Who | What |
|---|---|---|
| `/notify` (`status`) | allowed users | mode and its source, each service (on / disabled / not configured, its minimum severity), quiet hours, held items and the next card, retry queue, last test, trade management. Never shows URLs, tokens or the ntfy topic |
| `/notify prefs` | allowed users | the severity table, windows, per-service and per-symbol thresholds |
| `/notify test` | explicitly listed users | posts a "🧪 notification test" card to every enabled configured service from the dispatch thread; results appear in `/notify status`. No order, no MT5 call |
| `/notify_mode mode` | explicitly listed users | standard / intelligent / monitor (persisted; held cards are sent at the next pass after switching to standard) |
| `/trade_management` (`status`) | allowed users | ON/OFF, TSL mode, PLACE_ENABLED, last change; Enable / Disable buttons |
| `/trade_management enable` | explicitly listed (+ `TRADE_MANAGEMENT_USER_IDS`) | the bot loop issues a one-time nonce (30 s) → **Confirm** (only that user, one use) → enabled |
| `/trade_management disable` | same | immediate, no confirmation |

The **⚙️ CONTROLS** card (in `#updates`, edited in place, posted at start and after every change) carries
persistent **Enable trade management** / **Disable trade management** buttons (`aureon:tm:enable|disable`), routed
by custom id like the other buttons, so they keep working after a restart.

Authorization is one function, `alerts.may(user, channel, scope)`. The Discord thread checks it before the signed
command is sent, and the bot loop checks it again when the command runs.

## 6. Trade management switch

Management orders are the ones the bot sends about an existing position without a fresh order command: the stepped
trailing stop (`TSL_MODE=auto`) and the **Apply** button of a suggested step (`TSL_MODE=suggest`). All go through
`trader.modify_sl` (tighten only, verified by reading the position back).

- **OFF:**
  - Positions are still tracked: OPENED / CLOSED / AGAINST / MAX POINT / weekend cards continue.
  - Each would-be step is reported once as `⏸️ TSL step NOT applied — trade management is OFF`, recorded as an
    `sl_change` with `mode: "blocked"`, and invalidated (it is not applied later).
  - Apply is refused.
  - Broker-side SL/TP are never touched. Market data, labels and records are unaffected.
- **ON:** exactly the earlier TSL behaviour (auto or suggest). It never adds automatic entry or automatic closing.
- **Independent:** `/place` and the confirmed **Close** button stay governed only by `PLACE_ENABLED` (plus their
  existing account, risk and allowlist checks).
- **Pending invalidation:**
  - Every change bumps a generation number.
  - A TSL decision captures it, and the Apply button carries it (`aureon:apply:<ticket>:<sl>:<generation>`).
  - `trade_mgmt.check(token)` runs inside `trader.modify_sl` immediately before `order_send`. It refuses if
    management is OFF or the generation moved.
  - So a disable voids every pending request, and a later enable does not revive it. Apply buttons posted before
    this change have no generation and are refused.
- **Persistence:**
  - Atomic file with `.bak` and a history of every change (timestamp, user ID, previous, new). Every change is
    also logged as a WARNING, written as a `system` record and announced as a critical card.
  - Any problem reading the file, or a malformed `.env` value, means OFF.
  - A persisted OFF always wins.
  - A persisted ON is restored after a restart only with `TRADE_MANAGEMENT_RESTORE_ENABLED=1` or
    `TRADE_MANAGEMENT_ENABLED=true`; otherwise the bot starts OFF and says so.
  - An enable that cannot be saved is undone (fail closed).

## 7. Example notifications

Intelligent mode, a group card (Discord `#updates`):
```
XAUUSD 📋 UPDATES · 3 events
`14:02` • XAUUSD 🔴 M30 · GAP WIDENING ▼ SHORT
`14:02` • XAUUSD 🔴 M30 · PARALLEL RUNNING ▼ SHORT
`14:32` • XAUUSD 🟡 M30 · PULLBACK STARTED ▼ SHORT
Window: 10 Nov 14:02 → 14:32 IST
NORMAL · intelligent mode
```

A high card with context (footer shows the severity; quiet hours remove the mention):
```
XAUUSD 🔴 M30 · EMA 8/32 CROSS ▼ BEAR
Price 4091.20 · Candle closed 10 Nov 15:30 IST · Note: tag below 200 · Market now: …
Also since the last card: `15:01` • XAUUSD 🔴 M30 · WIDE GAP ▼ SHORT
Earlier this hour: `14:58` ❗ XAUUSD 🟡 M30 · PRE-CROSS 8/32 ▼ BEAR
13:00 server · HIGH · 🌙 quiet hours
```

Trade management OFF, a blocked step (`#positions`):
```
XAUUSD ⏸️ TSL step NOT applied — trade management is OFF
Ticket 7001 · SL stays 4060.00 · Step would be 4044.20
Trigger: close beyond 4046.10 after RE-ENTRY · pullback extreme 4039.20
Why: trade management is OFF (/trade_management enable). The broker-side SL/TP are unchanged; this step will not be applied later.
```

State change (critical, every service):
```
⚙️ TRADE MANAGEMENT ON
The bot may now send its trailing-stop orders.
Previous OFF · Now ON · By @owner · At 2026-11-23T18:40:12+00:00
```

## 8. Rollback

- **Notifications only:** `/notify_mode standard` (instant, persisted), or `NOTIFY_MODE=standard` and delete
  `logs/state/notify.json`. Standard mode is the earlier delivery path.
- **Trade management:** `/trade_management disable` is immediate. To return to the old always-on trailing stop
  without reverting code, set `TRADE_MANAGEMENT_ENABLED=true` (and keep `TSL_MODE` as before).
- **Code:** revert the merge commit (`git revert -m 1 <merge-sha>`) on the update branch; the watchdog fast-forwards
  to the revert. The new files (`logs/state/notify.json`, `logs/state/trade_management.json`, `logs/notify/`) are
  then unused and can be deleted. Old versions ignore the new `.env` keys. Apply buttons posted by the new version
  carry an extra id part; the old version reads only the ticket and stop, so they still work after a rollback (as
  before, only ever tightening).
