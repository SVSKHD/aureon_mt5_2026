"""All settings in one place. Secrets come from the project's .env file (never hard-code webhooks)."""
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("AUREON_ENV_FILE") or os.path.join(PROJECT_DIR, ".env")   # override only for tests


def _read_env_file(path: str) -> dict:
    """Parse KEY=VALUE lines from .env. Values stay in this dict only - os.environ is NOT modified."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:                  # utf-8-sig: tolerate a BOM from Notepad
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]                                  # "quoted" or 'quoted'
            elif " #" in val:
                val = val.split(" #", 1)[0].rstrip()             # inline comment on an unquoted value
            out[key] = val
    return out


_ENV = _read_env_file(ENV_FILE)


def env(key: str, default: str = "") -> str:
    """Value from .env only (system environment variables are ignored for now)."""
    val = _ENV.get(key, "")
    return val if val != "" else default


SYMBOL = env("SYMBOL", "XAUUSD")
TIMEFRAMES = ["M30"]
BAR_MINUTES = {"M15": 15, "M30": 30, "H1": 60, "H4": 240}

EMA_FAST, EMA_SLOW, EMA_TREND = 8, 32, 200
BARS = 1200                     # default history when there is no checkpoint; with one, fetch spans the whole gap
WARMUP_BARS = 1000              # bars fetched before the checkpoint: 5x EMA200 so it matches the MT5 chart to < 1 cent
MAX_REPLAY_BARS = 20000         # hard sanity cap (M30: ~1.1 years). MT5 'Max bars in chart' must allow it

PRE_CROSS_BARS = 3              # gap shrinking N bars in a row ...
PRE_CROSS_ATR_FRAC = 0.15       # ... and gap <= frac * ATR(14)
PARALLEL_200_WINDOW = 3         # EMA8 and EMA32 both cross EMA200 within N bars
ALERT_SINGLE_TREND_CROSS = True # also notify EMA8/200 and EMA32/200 crossings individually

# Narrow zone (chop): EMA8/32 gap <= NARROW_ATR_FRAC * ATR for NARROW_MIN_BARS consecutive bars
NARROW_ATR_FRAC = 0.25          # "tight" threshold
NARROW_MIN_BARS = 4             # M30 -> 2 hours glued together = a zone
NARROW_EXIT_FRAC = 0.50         # zone ends when gap expands past this * ATR

DEFAULT_LOOKBACK_BARS = 4       # first run with no checkpoint: scan last N bars (M30 -> 2 h)
MISSED_GRACE_SECONDS = 120      # a bar older than close + this is reported as "missed"
DIGEST_MAX_LINES = 20           # missed events after an outage are sent as ONE digest, at most this many lines

NEW_ALERT_TICK_SECONDS = 180    # a new price alert is checked on exact ticks for this long, then on M1 bars
PRICE_HISTORY_RETRIES = 3       # loops to retry fetching M1 history before reporting the gap and moving on
POLL_SECONDS = 10               # price alerts / health are checked this often
BAR_CLOSE_DELAY = 3             # seconds after a candle closes before reading it (the loop wakes exactly then)
TIMEZONE = "Asia/Kolkata"       # weekend pause + IST display
# MT5 clock = how far MT5's timestamps are from real UTC. This is only the STARTING value: the bot measures the
# real offset from the live feed and uses that if it differs (and tells you). SERVER_TZ follows daylight saving
# (Europe/Athens = UTC+2 winter / +3 summer). SERVER_UTC_OFFSET_HOURS, if set, wins over SERVER_TZ (0 = plain UTC).
# Check any time during market hours: python bot.py --check-time
SERVER_TZ = env("SERVER_TZ", "Europe/Athens")
SERVER_UTC_OFFSET_HOURS = None

# Weekend pause in TIMEZONE (IST), following the real gold week: Fri close is ~02:30/03:30 IST Saturday,
# Mon open is ~03:30/04:30 IST. (weekday 0=Mon ... 5=Sat, "HH:MM")
PAUSE_START = (5, "04:00")      # Saturday 04:00 IST
PAUSE_END = (0, "03:25")        # Monday 03:25 IST

# "Am I blind?" notices
BLIND_NOTICE_SECONDS = 180      # terminal/symbol/clock problem for this long -> one warning
STALE_NOTICE_SECONDS = 75 * 60  # no new tick for this long (longer than the daily break) -> one warning
ALIVE_PING_IST = "08:00"        # one "alive" card per day at this IST time ("" = off)
START_NOTICE_COOLDOWN = 600     # at most one "bot online" card per this many seconds
# Events that should buzz the phone (Discord mention / high-priority push)
PING_TYPES = {"CROSS", "CROSS_AFTER_TREND", "PRE_CROSS", "PARALLEL_TREND", "FAST_X_TREND", "SLOW_X_TREND"}

DISCORD_WEBHOOK = env("DISCORD_WEBHOOK")
SLACK_WEBHOOK = env("SLACK_WEBHOOK")
DISCORD_MENTION = env("DISCORD_MENTION")                        # optional: e.g. <@123456789012345678> or @everyone, added to signal alerts
NTFY_TOPIC = env("NTFY_TOPIC")                                  # optional: instant phone push via ntfy (pick a long private topic name)
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh")
DISCORD_BOT_TOKEN = env("DISCORD_BOT_TOKEN")                    # optional: lets you type "/alert 4120" in Discord
try:
    DISCORD_ALERT_CHANNEL_ID = int(env("DISCORD_ALERT_CHANNEL_ID", "0"))   # optional: only listen in this channel
except ValueError:
    raise SystemExit("DISCORD_ALERT_CHANNEL_ID in .env must be a number (right-click channel -> Copy Channel ID)")
try:                                                             # optional: only these Discord user IDs may use /alert
    DISCORD_ALLOWED_USER_IDS = {int(x) for x in env("DISCORD_ALLOWED_USER_IDS").replace(";", ",").split(",") if x.strip()}
except ValueError:
    raise SystemExit("DISCORD_ALLOWED_USER_IDS in .env must be numbers separated by commas")
# ---- chart snapshot attached to signal alerts (Discord) and returned by /chart ----
CHART_ENABLED = True
CHART_BARS = 80                 # candles shown
CHART_KEEP_DAYS = 7             # older chart files are deleted at startup

# ---- /place : manual orders from Discord. OFF unless PLACE_ENABLED=1 is in .env ----
def _num(key: str):
    """A number from .env, or None when the line is empty (= that limit is off)."""
    raw = env(key)
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{key} in .env must be a number")


PLACE_ENABLED = env("PLACE_ENABLED") == "1"
PLACE_ALLOW_REAL = env("PLACE_ALLOW_REAL") == "1"          # a REAL-money account is refused unless this is set
PLACE_ACCOUNT_LOGINS = {int(x) for x in env("PLACE_ACCOUNT_LOGINS").replace(";", ",").split(",") if x.strip().isdigit()}
#                                 optional: only these MT5 account numbers may be traded (empty = any)
# Optional limits, all in .env, empty = off. Money values are in the account currency.
PLACE_MAX_LOT = _num("PLACE_MAX_LOT")                    # biggest lot per order
PLACE_MAX_RISK = _num("PLACE_MAX_RISK")                  # biggest loss at the stop-loss per order
PLACE_MAX_POSITIONS = _num("PLACE_MAX_POSITIONS")        # most open positions on the symbol
PLACE_DAILY_LOSS_LIMIT = _num("PLACE_DAILY_LOSS_LIMIT")  # no new orders once today's closed loss reaches this
PLACE_DEVIATION_POINTS = 50     # max slippage accepted on a market order, in points
PLACE_MAGIC = 832200            # tag on every order this bot sends
PLACE_CONFIRM_SECONDS = 30      # the Confirm button is valid this long
COMMAND_MAX_AGE = 10            # a trade command older than this many seconds is refused, never run late

FAIL_NOTICE_COOLDOWN = 1800     # seconds between repeat "service X failed" notices
MAX_PENDING = 2000              # undelivered messages kept for retry (older ones are summarised, never silently dropped)

# ---------------------------------------------------------------- folders
# Everything the bot writes lives under logs/ (override root with AUREON_DATA):
#   logs/state/    state.json(.bak), alerts.json(.bak), heartbeat.json, watchdog.lock
#   logs/inbox/    *.cmd / *.reply  (CLI and Discord commands)
#   logs/alerts/   alerts-YYYY-MM-DD.log   one file per IST day, every alert text + KEY
#   logs/runtime/  bot-YYYY-MM-DD.log, watchdog-YYYY-MM-DD.log
DATA_DIR = env("AUREON_DATA", os.path.join(PROJECT_DIR, "logs"))
STATE_DIR = os.path.join(DATA_DIR, "state")
INBOX_DIR = os.path.join(DATA_DIR, "inbox")
ALERT_LOG_DIR = os.path.join(DATA_DIR, "alerts")
RUNTIME_LOG_DIR = os.path.join(DATA_DIR, "runtime")
CHART_DIR = os.path.join(DATA_DIR, "charts")
TRADE_LOG_DIR = os.path.join(DATA_DIR, "trades")
for _d in (STATE_DIR, INBOX_DIR, ALERT_LOG_DIR, RUNTIME_LOG_DIR, CHART_DIR, TRADE_LOG_DIR):
    os.makedirs(_d, exist_ok=True)
try:
    os.chmod(INBOX_DIR, 0o700)                                   # commands folder: this user only (no effect on Windows)
except OSError:
    pass

STATE_FILE = os.path.join(STATE_DIR, "state.json")
ALERTS_FILE = os.path.join(STATE_DIR, "alerts.json")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.json")
WATCHDOG_LOCK = os.path.join(STATE_DIR, "watchdog.lock")


def _ist_today() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")


def alerts_log_path(day: str | None = None) -> str:
    return os.path.join(ALERT_LOG_DIR, f"alerts-{day or _ist_today()}.log")


class DailyFileHandler(__import__("logging").Handler):
    """Writes to <dir>/<name>-YYYY-MM-DD.log, switching file at IST midnight."""
    def __init__(self, directory: str, name: str):
        super().__init__(); self.dir, self.name_, self._day, self._fh = directory, name, None, None

    def emit(self, record):
        try:
            day = _ist_today()
            if day != self._day:
                if self._fh: self._fh.close()
                self._fh = open(os.path.join(self.dir, f"{self.name_}-{day}.log"), "a", encoding="utf-8"); self._day = day
            self._fh.write(self.format(record) + "\n"); self._fh.flush()
        except Exception:
            self.handleError(record)


def migrate_legacy_files():
    """One-time: move files an older version left in the project root into logs/."""
    import shutil, glob as _g
    moves = {"state.json": STATE_DIR, "state.json.bak": STATE_DIR, "alerts.json": STATE_DIR, "alerts.json.bak": STATE_DIR,
             "heartbeat.json": STATE_DIR, "watchdog.lock": STATE_DIR, "watchdog.log": RUNTIME_LOG_DIR}
    for name, dest in moves.items():
        src = os.path.join(PROJECT_DIR, name)
        if os.path.exists(src) and not os.path.exists(os.path.join(dest, name)):
            shutil.move(src, os.path.join(dest, name))
    old_log = os.path.join(PROJECT_DIR, "alerts.log")
    if os.path.exists(old_log):
        shutil.move(old_log, os.path.join(ALERT_LOG_DIR, "alerts-legacy.log"))
    old_inbox = os.path.join(PROJECT_DIR, "alerts_inbox")
    if os.path.isdir(old_inbox):
        for f in _g.glob(os.path.join(old_inbox, "*")):
            shutil.move(f, os.path.join(INBOX_DIR, os.path.basename(f)))
        os.rmdir(old_inbox)


def validate() -> list[str]:
    """Human-readable config problems (empty = fine). Checked once at startup."""
    import re
    bad = []
    if not (DISCORD_WEBHOOK or SLACK_WEBHOOK or NTFY_TOPIC):
        bad.append(f"no delivery configured: set DISCORD_WEBHOOK, SLACK_WEBHOOK and/or NTFY_TOPIC in {ENV_FILE}")
    for tf in TIMEFRAMES:
        if tf not in BAR_MINUTES:
            bad.append(f"timeframe {tf} is not in BAR_MINUTES")
    for name, (wd, hhmm) in (("PAUSE_START", PAUSE_START), ("PAUSE_END", PAUSE_END)):
        if wd not in range(7) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm):
            bad.append(f"{name} must be (weekday 0-6, 'HH:MM')")
    if ALIVE_PING_IST and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ALIVE_PING_IST):
        bad.append("ALIVE_PING_IST must be 'HH:MM' or ''")
    if not (EMA_FAST < EMA_SLOW < EMA_TREND):
        bad.append("EMA periods must be FAST < SLOW < TREND")
    if PLACE_ENABLED and not (DISCORD_BOT_TOKEN and DISCORD_ALLOWED_USER_IDS):
        bad.append("PLACE_ENABLED=1 needs DISCORD_BOT_TOKEN and DISCORD_ALLOWED_USER_IDS (who may place orders)")
    if WARMUP_BARS < 3 * EMA_TREND:
        bad.append("WARMUP_BARS must be at least 3x EMA_TREND or EMA200 will not match the chart")
    if SERVER_UTC_OFFSET_HOURS is None and SERVER_TZ:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(SERVER_TZ); ZoneInfo(TIMEZONE)
        except Exception:
            bad.append(f"timezone data missing for {SERVER_TZ!r}/{TIMEZONE!r}: pip install tzdata")
    return bad
