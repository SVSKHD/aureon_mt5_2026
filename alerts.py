"""Price alerts. Only the bot loop writes alerts.json. The CLI and the Discord listener both drop
command files in logs/inbox/ and wait for the bot's reply file, so there is one writer.
    CLI:      python alerts.py add 4120.5 | list | remove 4120.5
    Discord:  /alert 4120.5   /alert remove 4120.5   /alerts        (needs DISCORD_BOT_TOKEN)
A triggered alert is kept under "triggered" until a notification is actually delivered."""
import glob, hashlib, hmac, json, os, secrets, sys, threading, time, uuid
import config as C

_EMPTY = {"pending": [], "triggered": []}
# Chart/quote/place commands are signed with a key that exists only in this process's memory. The Discord thread
# and the bot loop share it; nothing else can - so a file dropped into the inbox by another program is refused.
_SECRET = secrets.token_bytes(32)
_seen_ids: list[str] = []


def _sign(body: dict) -> str:
    return hmac.new(_SECRET, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def _verified(envelope) -> dict | None:
    """The command body if it carries this process's signature and was not seen before, else None."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("body"), dict):
        return None
    body = envelope["body"]
    if not hmac.compare_digest(str(envelope.get("sig", "")), _sign(body)) or body.get("id") in _seen_ids:
        return None
    _seen_ids.append(body.get("id")); del _seen_ids[:-500]
    return body


def _load():
    from notifier import load_json
    d, problem = load_json(C.ALERTS_FILE, _EMPTY)
    if problem:
        import logging; logging.getLogger("alerts").error(problem)
    return d


def _save(d):
    from notifier import save_json
    save_json(C.ALERTS_FILE, d)


def _now() -> float:
    return time.time()


# ---------- bot-side API ----------
def _sym(a: dict) -> str:
    return a.get("symbol") or C.SYMBOL                            # alerts saved by v2.1 belong to the primary symbol


def _same(a: dict, level: float, symbol: str) -> bool:
    return abs(a["level"] - level) < 1e-6 and _sym(a) == symbol


def add(level: float, price: float | None = None, symbol: str | None = None) -> str:
    symbol = symbol or C.SYMBOL
    d = _load()
    if any(_same(a, level, symbol) for a in d["pending"]):
        return f"Alert at {level:.2f} on {symbol} already exists."
    # 'last' = price at creation, so only movement AFTER creation can trigger it
    d["pending"].append({"level": level, "last": price, "created": _now(), "symbol": symbol}); _save(d)
    return f"✅ Alert set: notify when {symbol} reaches {level:.2f}"


def remove(level: float, symbol: str | None = None) -> str:
    symbol = symbol or C.SYMBOL
    d = _load(); n = len(d["pending"])
    d["pending"] = [a for a in d["pending"] if not _same(a, level, symbol)]; _save(d)
    return f"Removed {level:.2f} on {symbol}" if len(d["pending"]) < n else f"No alert at {level:.2f} on {symbol}"


def listing() -> str:
    d = _load()
    return "No pending alerts." if not d["pending"] else \
        "Pending: " + ", ".join(f"{_sym(a)} {a['level']:.2f}" for a in d["pending"])


def pending(symbol: str | None = None) -> list[dict]:
    return [a for a in _load()["pending"] if symbol is None or _sym(a) == symbol]


def check(last: float, m1: list | None = None, server_offset_s: float = 0.0,
          tick_ranges: dict | None = None, symbol: str | None = None) -> list[dict]:
    """Move touched levels of `symbol` to 'triggered'. An alert only ever sees prices from AFTER it was created:
      - the move between the last seen price and the live price,
      - M1 bars (time, low, high) that OPENED after its creation,
      - tick_ranges[level] = exact (low, high) since creation, supplied by the bot for a new alert's first
        minutes (the M1 bar it was created in also holds older prices, so that bar is never used)."""
    symbol = symbol or C.SYMBOL
    d = _load(); hit, keep = [], []
    for a in d["pending"]:
        if _sym(a) != symbol:
            keep.append(a); continue
        if a["last"] is None:                                     # created without a price: arm now, judge next time
            a["last"] = last; keep.append(a); continue
        lo, hi = min(a["last"], last), max(a["last"], last)
        created_srv = a.get("created", 0) + server_offset_s       # M1 times are MT5-stamped epochs
        for t, l, h in (m1 or []):
            if t >= created_srv:
                lo, hi = min(lo, l), max(hi, h)
        tr = (tick_ranges or {}).get(a["level"])
        if tr:
            lo, hi = min(lo, tr[0]), max(hi, tr[1])
        if lo <= a["level"] <= hi:
            hit.append({"level": a["level"], "price": last, "ts": time.time(), "symbol": symbol})
        else:
            a["last"] = last; keep.append(a)
    d["pending"], d["triggered"] = keep, d["triggered"] + hit
    _save(d)
    return [t for t in d["triggered"] if _sym(t) == symbol]      # includes earlier ones not yet delivered


def ack(level: float, symbol: str | None = None):
    """Call only after the notification was delivered."""
    symbol = symbol or C.SYMBOL
    d = _load()
    d["triggered"] = [t for t in d["triggered"] if not _same(t, level, symbol)]; _save(d)


def handle_command(text: str, price: float | None = None, price_of=None) -> str:
    """add <price> [symbol] | remove <price> [symbol] | list. price_of(symbol) -> live price (bot loop)."""
    parts = text.strip().split()
    try:
        if not parts or parts[0] in ("list", "ls"):
            return listing()
        if parts[0] in ("add", "remove", "rm", "del"):
            verb, rest = parts[0], parts[1:]
        else:
            verb, rest = "add", parts
        level = float(rest[0])
        symbol = (rest[1] if len(rest) > 1 else C.SYMBOL).upper()
        if symbol not in C.SYMBOLS:
            return f"unknown symbol {symbol}"
        if not C.is_enabled(symbol):
            return f"{symbol} is disabled in config"
        if verb == "add":
            return add(level, price_of(symbol) if price_of else price, symbol)
        return remove(level, symbol)
    except (IndexError, ValueError):
        return "Usage: add <price> [symbol] | remove <price> [symbol] | list"


def inbox_waiting() -> bool:
    return bool(glob.glob(os.path.join(C.INBOX_DIR, "*.cmd")))


def process_inbox(price: float | None = None, json_handler=None, price_of=None) -> list[str]:
    """Bot: apply every command file, write a .reply next to it. Returns replies for logging.
    Plain text = price-alert command. JSON = handed to json_handler (chart / quote / place); such a command is
    DELETED BEFORE it runs, so a crash or restart can never run it a second time."""
    os.makedirs(C.INBOX_DIR, exist_ok=True)
    out = []
    for path in sorted(glob.glob(os.path.join(C.INBOX_DIR, "*.cmd"))):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if text.lstrip().startswith("{"):
                os.remove(path)                                   # claim first
                body = _verified(json.loads(text))
                if body is None:
                    reply = json.dumps({"ok": False, "text": "⛔ Refused — this command did not come from the bot's own Discord connection."})
                else:
                    reply = json.dumps(json_handler(body) if json_handler else
                                       {"ok": False, "text": "this command is not available"})
            else:
                reply = handle_command(text, price, price_of)
        except Exception as e:
            reply = f"error: {e}"
        with open(path[:-4] + ".tmp", "w", encoding="utf-8") as f:
            f.write(reply)
        os.replace(path[:-4] + ".tmp", path[:-4] + ".reply")
        if os.path.exists(path):
            os.remove(path)
        out.append(reply)
    return out


# ---------- command senders (CLI + Discord) ----------
def envelope(cmd: str, payload: dict | None = None, ts: float | None = None) -> dict:
    body = {**(payload or {}), "cmd": cmd, "ts": _now() if ts is None else ts, "id": uuid.uuid4().hex}
    return {"body": body, "sig": _sign(body)}


def request(cmd: str, payload: dict | None = None, wait_s: float = 8.0) -> dict:
    """Send a JSON command (chart / quote / place) to the bot loop and return its reply dict.
    If the bot does not pick it up in time the command is WITHDRAWN, so it can never run late."""
    os.makedirs(C.INBOX_DIR, exist_ok=True)
    base = os.path.join(C.INBOX_DIR, uuid.uuid4().hex)
    with open(base + ".tmp", "w", encoding="utf-8") as f:
        json.dump(envelope(cmd, payload), f)
    os.replace(base + ".tmp", base + ".cmd")

    def read_reply():
        if not os.path.exists(base + ".reply"):
            return None
        with open(base + ".reply", encoding="utf-8") as f:
            raw = f.read()
        os.remove(base + ".reply")
        try:
            return json.loads(raw)
        except ValueError:
            return {"ok": False, "text": raw}

    deadline = time.time() + wait_s
    while time.time() < deadline:
        r = read_reply()
        if r is not None:
            return r
        time.sleep(0.2)
    try:
        os.remove(base + ".cmd")                                  # still unclaimed -> withdraw it
        return {"ok": False, "text": "❌ NOT done — the bot did not pick the command up (paused, blind or stopped)."}
    except FileNotFoundError:
        pass                                                      # the bot claimed it: give it a little longer
    deadline = time.time() + 20
    while time.time() < deadline:
        r = read_reply()
        if r is not None:
            return r
        time.sleep(0.2)
    return {"ok": False, "text": "⚠️ No answer from the bot. CHECK MT5 for an open position before trying again."}


def submit(cmd: str, wait_s: float | None = None) -> str:
    """Drop a command in the inbox and wait for the bot's reply. Used by the CLI and the Discord listener."""
    os.makedirs(C.INBOX_DIR, exist_ok=True)
    base = os.path.join(C.INBOX_DIR, uuid.uuid4().hex)
    with open(base + ".tmp", "w", encoding="utf-8") as f:
        f.write(cmd)
    os.replace(base + ".tmp", base + ".cmd")                      # the bot never sees a half-written command
    deadline = time.time() + (C.POLL_SECONDS * 1.5 if wait_s is None else wait_s)
    while time.time() < deadline:
        if os.path.exists(base + ".reply"):
            with open(base + ".reply", encoding="utf-8") as f:
                r = f.read()
            os.remove(base + ".reply"); return r
        time.sleep(1)
    return "Queued — the bot is paused or busy; it will apply this on its next loop."


def discord_to_cmd(text: str) -> str | None:
    """Map a Discord message to an inbox command, or None if it isn't one.
    /alert 4120.5 | /alert add 4120.5 | /alert remove 4120.5 | /alerts | /alert list"""
    parts = text.strip().split()
    if not parts:
        return None
    head = parts[0].lower()
    if head == "/alerts":
        return "list"
    if head != "/alert":
        return None
    return " ".join(parts[1:]) if len(parts) > 1 else "list"


def may(user_id, channel_id=None, scope: str = "read") -> bool:
    """THE authorization rule. The Discord thread checks it before a command is sent, and the bot loop checks it
    again when the signed command runs (channel_id None there = the channel was already checked in Discord).
      read    the alert channel (if set) and an allowed user (if the list is set)
      trade   the user must be EXPLICITLY listed in DISCORD_ALLOWED_USER_IDS (orders, Apply, Close, settings)
      manage  trade + in TRADE_MANAGEMENT_USER_IDS when that list is set (the trade management switch)"""
    if channel_id is not None and C.DISCORD_ALERT_CHANNEL_ID and channel_id != C.DISCORD_ALERT_CHANNEL_ID:
        return False
    if scope == "read":
        return not C.DISCORD_ALLOWED_USER_IDS or user_id in C.DISCORD_ALLOWED_USER_IDS
    if not (C.DISCORD_ALLOWED_USER_IDS and user_id in C.DISCORD_ALLOWED_USER_IDS):
        return False
    if scope == "manage":
        return not C.TRADE_MANAGEMENT_USER_IDS or user_id in C.TRADE_MANAGEMENT_USER_IDS
    return scope == "trade"


def authorised(user_id: int, channel_id: int) -> bool:
    """A command is accepted only in the alert channel (if set) and only from an allowed user (if set)."""
    return may(user_id, channel_id, "read")


def can_trade(user_id: int, channel_id: int) -> bool:
    """Orders are stricter: the user must be explicitly listed in DISCORD_ALLOWED_USER_IDS."""
    return may(user_id, channel_id, "trade")


def can_manage(user_id: int, channel_id: int) -> bool:
    """The trade management switch: can_trade, and in TRADE_MANAGEMENT_USER_IDS if that list is set."""
    return may(user_id, channel_id, "manage")


EXTRA_COMMANDS: list = [           # (name, description): simple [symbol] commands; the bot loop does the work
    ("positions", "Open positions, stops, next trailing step, account totals"),
    ("monitor", "Live monitor card for a symbol (stays on until /unmonitor)"),
    ("unmonitor", "Stop the live monitor card"),
    ("last-week", "Last completed Mon-Fri: crosses, 200 crosses, positions, P&L, week chart"),
]
NOTIFY_ACTIONS = ("status", "prefs", "test")                      # /notify
TM_ACTIONS = ("status", "enable", "disable")                      # /trade_management


def chunks(text: str, size: int = 1990) -> list[str]:
    """Split a long reply on line breaks so each piece fits one Discord message."""
    out, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > size:
            out.append(line[:size]); line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else []) or [""]


def build_discord_client():
    """The Discord side: real slash commands (with drop-downs) that only talk to the bot loop through the inbox.
        /chart                       market snapshot now
        /place side lot sl tp [filling]   preview -> Confirm button -> market order with SL and TP
        /alert price   /alert_remove price   /alerts
    Returns the client (not yet running)."""
    import asyncio, discord
    from discord import app_commands

    intents = discord.Intents.default(); intents.message_content = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    log = __import__("logging").getLogger("alerts")

    async def ask(cmd, payload=None, wait_s=8.0):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: request(cmd, payload, wait_s))

    async def run_text(cmd):
        return await asyncio.get_running_loop().run_in_executor(None, submit, cmd)

    def files_of(reply):
        paths = reply.get("images") or ([reply["image"]] if reply.get("image") else [])
        return [discord.File(p, filename=f"chart{i}.png") for i, p in enumerate(paths) if p and os.path.exists(p)]

    SYMBOL_CHOICES = [app_commands.Choice(name=s + ("" if C.is_enabled(s) else " (disabled)"), value=s) for s in C.SYMBOLS]

    async def answer(inter, reply, view=None):
        parts, files = chunks(reply.get("text", "no reply")), files_of(reply)
        for k, part in enumerate(parts):
            kw = {}
            if k == len(parts) - 1:                               # pictures and buttons on the last piece
                if files:
                    kw["files"] = files
                if view:
                    kw["view"] = view
            await inter.followup.send(part, **kw)

    async def allowed(inter, trade=False, scope: str | None = None) -> bool:
        ok = may(inter.user.id, inter.channel_id, scope or ("trade" if trade else "read"))
        if not ok:
            await inter.response.send_message("⛔ You are not allowed to do that here.", ephemeral=True)
        return ok

    @tree.command(name="chart", description="Market snapshot now: candles with EMA 8/32/200")
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    async def chart_cmd(inter: discord.Interaction, symbol: app_commands.Choice[str] | None = None):
        if not await allowed(inter):
            return
        await inter.response.defer()
        await answer(inter, await ask("chart", {"symbol": symbol.value if symbol else None}))

    @tree.command(name="status", description="Chart, market status, version and monitor state (one card per symbol)")
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    async def status_cmd(inter: discord.Interaction, symbol: app_commands.Choice[str] | None = None):
        if not await allowed(inter):
            return
        await inter.response.defer()
        await answer(inter, await ask("status", {"symbol": symbol.value if symbol else None}))

    for name, desc in EXTRA_COMMANDS:                               # simple commands added by features
        def make(cmd_name):
            @app_commands.choices(symbol=SYMBOL_CHOICES)
            async def simple(inter: discord.Interaction, symbol: app_commands.Choice[str] | None = None):
                if not await allowed(inter):
                    return
                await inter.response.defer()
                reply = await ask(cmd_name.replace("-", "_"), {"symbol": symbol.value if symbol else None,
                                                               "user": inter.user.id}, wait_s=30.0)
                await answer(inter, reply, button_view(reply.get("buttons")))
            return simple
        tree.command(name=name, description=desc)(make(name))

    class ConfirmView(discord.ui.View):
        """One explicit confirmation, used by every confirm step (order, close, trade management): only the user
        who asked can press it, it works once (the flag is set before any await, so clicks cannot race), Cancel
        uses it up too, and it expires. `run()` sends the confirmed command and returns the bot's reply."""
        def __init__(self, user_id: int, run, *, label: str, cancelled: str, expired: str,
                     not_yours: str = "Not yours, or already handled.", already: str | None = None,
                     ephemeral: bool = False, timeout: float = C.PLACE_CONFIRM_SECONDS):
            super().__init__(timeout=timeout)
            self.user_id, self.run, self.message, self.used = user_id, run, None, False
            self.texts = {"cancelled": cancelled, "expired": expired, "not_yours": not_yours,
                          "already": already or not_yours}
            self.ephemeral = ephemeral
            self.confirm = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
            self.cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            self.confirm.callback, self.cancel.callback = self._confirm, self._cancel
            self.add_item(self.confirm); self.add_item(self.cancel)

        async def interaction_check(self, inter: discord.Interaction) -> bool:
            if inter.user.id != self.user_id:
                await inter.response.send_message(self.texts["not_yours"], ephemeral=True)
                return False
            if self.used:                                          # a second click while the first is being handled
                await inter.response.send_message(self.texts["already"], ephemeral=True)
                return False
            self.used = True                                       # set before any await: clicks cannot race
            return True

        async def finish(self, inter, text):
            for item in self.children:
                item.disabled = True
            self.stop()
            await inter.edit_original_response(content=text, view=self)

        async def _confirm(self, inter: discord.Interaction):
            await (inter.response.defer(ephemeral=True) if self.ephemeral else inter.response.defer())
            reply = await self.run()
            await self.finish(inter, reply.get("text", "no reply"))

        async def _cancel(self, inter: discord.Interaction):
            await (inter.response.defer(ephemeral=True) if self.ephemeral else inter.response.defer())
            await self.finish(inter, self.texts["cancelled"])

        async def on_timeout(self):
            self.used = True
            for item in self.children:
                item.disabled = True
            if self.message:
                try:
                    await self.message.edit(content=self.texts["expired"], view=self)
                except discord.HTTPException:
                    pass

    def Confirm(user_id: int, payload: dict):
        """/place: Confirm order -> the order is sent (trader.py)."""
        return ConfirmView(user_id, lambda: ask("place", payload), label="Confirm order",
                           cancelled="Cancelled — nothing was placed.", expired="⌛ Expired — nothing was placed.",
                           not_yours="Only the person who typed /place can confirm it.",
                           already="Already handled — one order per /place.")

    @tree.command(name="place", description="Place a market order with stop-loss and take-profit (you confirm before it is sent)")
    @app_commands.describe(symbol="Symbol", side="Buy or sell", lot="Lot size, e.g. 0.5",
                           sl="Stop-loss: distance from entry (20 = $20 away) or an exact price (4095.5)",
                           tp="Take-profit: distance from entry (50 = $50 away) or an exact price (4165.5)",
                           filling="Order filling type (Auto = whatever your broker supports)")
    @app_commands.choices(side=[app_commands.Choice(name="Buy", value="buy"), app_commands.Choice(name="Sell", value="sell")],
                          filling=[app_commands.Choice(name="Auto (broker default)", value="auto"),
                                   app_commands.Choice(name="FOK — fill or kill", value="fok"),
                                   app_commands.Choice(name="IOC — immediate or cancel", value="ioc"),
                                   app_commands.Choice(name="Return", value="return")])
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    async def place_cmd(inter: discord.Interaction, symbol: app_commands.Choice[str], side: app_commands.Choice[str],
                        lot: app_commands.Range[float, 0.01, 1000.0], sl: app_commands.Range[float, 0.01, None],
                        tp: app_commands.Range[float, 0.01, None], filling: app_commands.Choice[str] | None = None):
        if not await allowed(inter, trade=True):
            return
        await inter.response.defer()
        payload = {"side": side.value, "lot": lot, "sl": sl, "tp": tp, "filling": filling.value if filling else "auto",
                   "user": inter.user.id, "symbol": symbol.value}
        quote = await ask("quote", payload)
        if not quote.get("ok"):
            await inter.followup.send(quote.get("text", "no reply")); return
        view = Confirm(inter.user.id, payload)
        view.message = await inter.followup.send(f"**Confirm within {C.PLACE_CONFIRM_SECONDS} s**\n{quote['text']}", view=view, wait=True)

    @tree.command(name="alert", description="Notify me when price reaches a level")
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    async def alert_cmd(inter: discord.Interaction, price: float, symbol: app_commands.Choice[str] | None = None):
        if await allowed(inter):
            await inter.response.defer()
            await inter.followup.send(await run_text(f"add {price} {symbol.value if symbol else C.SYMBOL}"))

    @tree.command(name="alert_remove", description="Remove a price alert")
    @app_commands.choices(symbol=SYMBOL_CHOICES)
    async def alert_remove_cmd(inter: discord.Interaction, price: float, symbol: app_commands.Choice[str] | None = None):
        if await allowed(inter):
            await inter.response.defer()
            await inter.followup.send(await run_text(f"remove {price} {symbol.value if symbol else C.SYMBOL}"))

    @tree.command(name="alerts", description="List pending price alerts")
    async def alerts_cmd(inter: discord.Interaction):
        if await allowed(inter):
            await inter.response.defer(); await inter.followup.send(await run_text("list"))

    # ---------------- buttons: one router for every "aureon:<action>:<args>" custom id ----------------------------
    STYLE = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
             "danger": discord.ButtonStyle.danger, "success": discord.ButtonStyle.success}

    def button_view(buttons):
        """A view for posted cards. timeout=None and fixed custom ids: the buttons keep working after a restart
        because on_interaction routes them, not this object."""
        if not buttons:
            return None
        view = discord.ui.View(timeout=None)
        for b in buttons[:5]:
            view.add_item(discord.ui.Button(label=b["label"], custom_id=b["id"][:100],
                                            style=STYLE.get(b.get("style"), discord.ButtonStyle.secondary)))
        return view

    def CloseConfirm(user_id: int, payload: dict):
        """Close position from a MAX POINT card: one more explicit Confirm, 30 s, one use."""
        return ConfirmView(user_id, lambda: ask("close", payload), label="Confirm close", ephemeral=True,
                           cancelled="Cancelled — the position is untouched.",
                           expired="⌛ Expired — the position is untouched.")

    async def tm_action(inter: discord.Interaction, action: str):
        """/trade_management and the Enable/Disable buttons. Enabling = a nonce from the bot loop + Confirm."""
        if action == "status":
            await inter.response.defer()
            reply = await ask("trade_management", {"action": "status", "user": inter.user.id,
                                                   "channel": inter.channel_id})
            await answer(inter, reply, button_view(reply.get("buttons")))
            return
        if not await allowed(inter, scope="manage"):
            return
        base = {"user": inter.user.id, "channel": inter.channel_id}
        await inter.response.defer(ephemeral=True)
        if action == "disable":                                    # immediate, no confirmation needed
            reply = await ask("trade_management", {**base, "action": "disable"})
            await inter.followup.send(str(reply.get("text", "no reply"))[:1990], ephemeral=True)
            return
        if action != "enable":
            await inter.followup.send("Unknown action.", ephemeral=True)
            return
        req = await ask("trade_management", {**base, "action": "enable_request"})
        if not req.get("ok") or not req.get("nonce"):
            await inter.followup.send(str(req.get("text", "no reply"))[:1990], ephemeral=True)
            return
        view = ConfirmView(inter.user.id, lambda: ask("trade_management", {**base, "action": "enable_confirm",
                                                                          "nonce": req["nonce"]}),
                           label="Confirm: ENABLE trade management", ephemeral=True,
                           cancelled="Cancelled — trade management is unchanged.",
                           expired="⌛ Expired — trade management is unchanged.",
                           timeout=C.TRADE_MANAGEMENT_CONFIRM_SECONDS)
        view.message = await inter.followup.send(req["text"], view=view, ephemeral=True, wait=True)

    @tree.command(name="trade_management", description="Trade management (automatic stop moves): status, enable, disable")
    @app_commands.choices(action=[app_commands.Choice(name=a.capitalize(), value=a) for a in TM_ACTIONS])
    async def tm_cmd(inter: discord.Interaction, action: app_commands.Choice[str] | None = None):
        if not await allowed(inter):
            return
        await tm_action(inter, action.value if action else "status")

    @tree.command(name="notify", description="Notifications: status (mode, services), prefs, or test every channel")
    @app_commands.choices(action=[app_commands.Choice(name=a.capitalize(), value=a) for a in NOTIFY_ACTIONS])
    async def notify_cmd(inter: discord.Interaction, action: app_commands.Choice[str] | None = None):
        act = action.value if action else "status"
        if not await allowed(inter, trade=act == "test"):
            return
        await inter.response.defer()
        await answer(inter, await ask("notify", {"action": act, "user": inter.user.id, "channel": inter.channel_id},
                                      wait_s=15.0))

    @tree.command(name="notify_mode", description="Switch the notification mode (standard = unchanged behaviour)")
    @app_commands.choices(mode=[app_commands.Choice(name=m.capitalize(), value=m) for m in C.NOTIFY_MODES])
    async def notify_mode_cmd(inter: discord.Interaction, mode: app_commands.Choice[str]):
        if not await allowed(inter, trade=True):
            return
        await inter.response.defer()
        await answer(inter, await ask("notify_mode", {"mode": mode.value, "user": inter.user.id,
                                                      "channel": inter.channel_id}))

    async def simple_reply(inter, cmd: str, payload: dict | None = None, scope: str = "read", wait_s: float = 15.0):
        """One shared path for the read-only / single-step commands: permission, defer, signed request, answer."""
        if not await allowed(inter, scope=scope):
            return
        await inter.response.defer()
        reply = await ask(cmd, {**(payload or {}), "user": inter.user.id, "channel": inter.channel_id}, wait_s=wait_s)
        await answer(inter, reply, button_view(reply.get("buttons")))

    @tree.command(name="mt5_health", description="MT5 connection health: state, cause, account check, order permission")
    async def mt5_health_cmd(inter: discord.Interaction):
        await simple_reply(inter, "mt5_health")

    @tree.command(name="mt5_connection", description="MT5 connection timeline: disconnections, attempts, restarts, recoveries")
    async def mt5_connection_cmd(inter: discord.Interaction):
        await simple_reply(inter, "mt5_connection")

    @tree.command(name="mt5_reconnect", description="Reconnect to MT5 now and re-check the account (never sends an order)")
    async def mt5_reconnect_cmd(inter: discord.Interaction):
        await simple_reply(inter, "mt5_reconnect", scope="trade", wait_s=30.0)

    @tree.command(name="replay_status", description="What the Replay Lab (HISTORICAL REPLAY) is doing on this PC")
    async def replay_status_cmd(inter: discord.Interaction):
        await simple_reply(inter, "replay_status")

    @tree.command(name="notification_mode", description="Notification mode: show it, or switch it (same as /notify + /notify_mode)")
    @app_commands.choices(mode=[app_commands.Choice(name=m.capitalize(), value=m) for m in C.NOTIFY_MODES])
    async def notification_mode_cmd(inter: discord.Interaction, mode: app_commands.Choice[str] | None = None):
        await simple_reply(inter, "notification_mode", {"mode": mode.value} if mode else {"action": "status"},
                           scope="trade" if mode else "read")

    TRADE_ACTIONS = {"apply", "close"}                              # need the user listed in DISCORD_ALLOWED_USER_IDS

    async def on_button(inter: discord.Interaction, custom_id: str):
        parts = custom_id.split(":")
        action, args = parts[1], parts[2:]
        if action == "tm":                                        # persistent Enable / Disable on the controls card
            await tm_action(inter, args[0] if args else "status")
            return
        if not await allowed(inter, trade=action in TRADE_ACTIONS):
            return
        if action == "close":                                     # Confirm step first, then trader.py closes it
            payload = {"ticket": int(args[0]), "user": inter.user.id}
            await inter.response.send_message(
                f"**Close position {args[0]}?** Confirm within {C.PLACE_CONFIRM_SECONDS} s.",
                view=CloseConfirm(inter.user.id, payload), ephemeral=True)
            return
        if action == "refresh":                                   # edit the same message
            await inter.response.defer()
            reply = await ask(args[0], {"user": inter.user.id, "symbol": args[1] if len(args) > 1 else None}, wait_s=30)
            await inter.edit_original_response(content=str(reply.get("text", "no reply"))[:1990],
                                               view=button_view(reply.get("buttons")))
            return
        await inter.response.defer(ephemeral=True)
        payload = {"user": inter.user.id, "args": args}
        if action in ("monitor", "unmonitor"):
            payload["symbol"] = args[0]
        if action in ("apply", "keep"):
            try:
                payload["ticket"] = int(args[0])
                if len(args) > 1:
                    payload["sl"] = float(args[1])
                if len(args) > 2:
                    payload["gen"] = int(args[2])              # the trade management generation of the suggestion
            except (IndexError, ValueError):
                await inter.followup.send("Malformed button.", ephemeral=True)
                return
        reply = await ask(action, payload)
        await inter.followup.send(str(reply.get("text", "no reply"))[:1990], ephemeral=True)

    @client.event
    async def on_interaction(inter: discord.Interaction):
        cid = (inter.data or {}).get("custom_id", "") if inter.type == discord.InteractionType.component else ""
        if cid.startswith("aureon:"):
            try:
                await on_button(inter, cid)
            except Exception:
                log.exception("button %s failed", cid)

    # ---------------- outbox: cards from the bot loop (buttons, edit-in-place) -----------------------------------
    def load_map():
        try:
            with open(C.DISCORD_MESSAGES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_map(m):
        tmp = C.DISCORD_MESSAGES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f)
        os.replace(tmp, C.DISCORD_MESSAGES_FILE)

    async def post_outbox_item(item: dict):
        from notifier import _discord
        payload = _discord(item)
        embed = discord.Embed.from_dict(payload["embeds"][0])
        content = payload.get("content")
        image = item.get("image")
        files = [discord.File(image, filename="chart.png")] if image and os.path.exists(image) else []
        if not files:
            embed.set_image(url=None)
        chan = client.get_channel(int(item["channel_id"])) or await client.fetch_channel(int(item["channel_id"]))
        view = button_view(item.get("buttons"))
        key = item.get("upsert")
        if key:
            m = load_map()
            if key in m:
                try:
                    msg = await chan.fetch_message(int(m[key]))
                    await msg.edit(content=content, embed=embed, attachments=files, view=view)
                    return
                except discord.NotFound:
                    pass                                          # deleted by someone: post a fresh one
            kw = {"embed": embed, "files": files, "view": view} if view else {"embed": embed, "files": files}
            msg = await chan.send(content=content, **kw)
            m[key] = msg.id; save_map(m)
            return
        kw = {"embed": embed, "files": files}
        if view:
            kw["view"] = view
        await chan.send(content=content, allowed_mentions=discord.AllowedMentions.all(), **kw)

    async def outbox_loop():
        while not client.is_closed():
            for path in sorted(glob.glob(os.path.join(C.OUTBOX_DIR, "*.json"))):
                try:
                    with open(path, encoding="utf-8") as f:
                        item = json.load(f)
                except (OSError, ValueError):
                    os.remove(path); continue
                try:
                    await post_outbox_item(item)
                    os.remove(path)
                except discord.HTTPException as e:
                    log.error("outbox post failed (%s), will retry: %s", e, item.get("title"))
                    break                                         # keep order; try again next round
                except Exception:
                    log.exception("outbox item dropped: %s", item.get("title"))
                    os.remove(path)
            await asyncio.sleep(1)

    started = {"outbox": False}

    @client.event
    async def on_ready():
        if not started["outbox"]:
            started["outbox"] = True
            client.loop.create_task(outbox_loop())
        for guild in client.guilds:                                # per-server sync = commands appear immediately
            try:
                tree.copy_global_to(guild=guild); await tree.sync(guild=guild)
            except discord.HTTPException as e:
                log.error("Could not register slash commands in %s: %s (re-invite the bot with the "
                          "'applications.commands' scope)", guild.name, e)
        log.info("Discord ready as %s — %d slash commands", client.user, len(tree.get_commands()))

    @client.event
    async def on_message(m):                                       # typed fallback: "/alert 4120" as plain text
        if m.author.bot:
            return
        cmd = discord_to_cmd(m.content)
        if cmd is None:
            return
        if not authorised(m.author.id, m.channel.id):
            return
        await m.channel.send(await run_text(cmd))

    client.tree, client.Confirm, client.CloseConfirm, client.on_button = tree, Confirm, CloseConfirm, on_button
    client.ConfirmView, client.tm_action = ConfirmView, tm_action
    client.button_view, client.post_outbox_item = button_view, post_outbox_item
    return client


def start_discord_listener():
    """Runs the Discord client in a daemon thread if DISCORD_BOT_TOKEN is set."""
    if not C.DISCORD_BOT_TOKEN:
        return False
    try:
        import discord  # noqa: F401
    except ImportError:
        import logging; logging.getLogger("alerts").error("DISCORD_BOT_TOKEN set but discord.py missing: pip install discord.py")
        return False

    def run():
        build_discord_client().run(C.DISCORD_BOT_TOKEN, log_handler=None)

    threading.Thread(target=run, daemon=True, name="discord-listener").start()
    return True


if __name__ == "__main__":
    print(submit(" ".join(sys.argv[1:])))
