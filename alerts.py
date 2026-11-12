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
def add(level: float, price: float | None = None) -> str:
    d = _load()
    if any(abs(a["level"] - level) < 1e-6 for a in d["pending"]):
        return f"Alert at {level:.2f} already exists."
    # 'last' = price at creation, so only movement AFTER creation can trigger it
    d["pending"].append({"level": level, "last": price, "created": _now()}); _save(d)
    return f"✅ Alert set: notify when {C.SYMBOL} reaches {level:.2f}"


def remove(level: float) -> str:
    d = _load(); n = len(d["pending"])
    d["pending"] = [a for a in d["pending"] if abs(a["level"] - level) > 1e-6]; _save(d)
    return f"Removed {level:.2f}" if len(d["pending"]) < n else f"No alert at {level:.2f}"


def listing() -> str:
    d = _load()
    return "No pending alerts." if not d["pending"] else \
        "Pending: " + ", ".join(f"{a['level']:.2f}" for a in d["pending"])


def pending() -> list[dict]:
    return _load()["pending"]


def check(last: float, m1: list | None = None, server_offset_s: float = 0.0,
          tick_ranges: dict | None = None) -> list[dict]:
    """Move touched levels to 'triggered'. An alert only ever sees prices from AFTER it was created:
      - the move between the last seen price and the live price,
      - M1 bars (time, low, high) that OPENED after its creation,
      - tick_ranges[level] = exact (low, high) since creation, supplied by the bot for a new alert's first
        minutes (the M1 bar it was created in also holds older prices, so that bar is never used)."""
    d = _load(); hit, keep = [], []
    for a in d["pending"]:
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
            hit.append({"level": a["level"], "price": last, "ts": time.time()})
        else:
            a["last"] = last; keep.append(a)
    d["pending"], d["triggered"] = keep, d["triggered"] + hit
    _save(d)
    return d["triggered"]                      # includes earlier ones not yet delivered


def ack(level: float):
    """Call only after the notification was delivered."""
    d = _load()
    d["triggered"] = [t for t in d["triggered"] if abs(t["level"] - level) > 1e-6]; _save(d)


def handle_command(text: str, price: float | None = None) -> str:
    parts = text.strip().split()
    try:
        if not parts or parts[0] in ("list", "ls"):
            return listing()
        if parts[0] in ("remove", "rm", "del"):
            return remove(float(parts[1]))
        if parts[0] == "add":
            return add(float(parts[1]), price)
        return add(float(parts[0]), price)
    except (IndexError, ValueError):
        return "Usage: add <price> | remove <price> | list"


def inbox_waiting() -> bool:
    return bool(glob.glob(os.path.join(C.INBOX_DIR, "*.cmd")))


def process_inbox(price: float | None = None, json_handler=None) -> list[str]:
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
                reply = handle_command(text, price)
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


def authorised(user_id: int, channel_id: int) -> bool:
    """A command is accepted only in the alert channel (if set) and only from an allowed user (if set)."""
    if C.DISCORD_ALERT_CHANNEL_ID and channel_id != C.DISCORD_ALERT_CHANNEL_ID:
        return False
    return not C.DISCORD_ALLOWED_USER_IDS or user_id in C.DISCORD_ALLOWED_USER_IDS


def can_trade(user_id: int, channel_id: int) -> bool:
    """Orders are stricter: the user must be explicitly listed in DISCORD_ALLOWED_USER_IDS."""
    return bool(C.DISCORD_ALLOWED_USER_IDS) and user_id in C.DISCORD_ALLOWED_USER_IDS and authorised(user_id, channel_id)


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
        path = reply.get("image")
        return [discord.File(path, filename="chart.png")] if path and os.path.exists(path) else []

    async def allowed(inter, trade=False) -> bool:
        ok = (can_trade if trade else authorised)(inter.user.id, inter.channel_id)
        if not ok:
            await inter.response.send_message("⛔ You are not allowed to do that here.", ephemeral=True)
        return ok

    @tree.command(name="chart", description="Market snapshot now: candles with EMA 8/32/200")
    async def chart_cmd(inter: discord.Interaction):
        if not await allowed(inter):
            return
        await inter.response.defer()
        reply = await ask("chart")
        files = files_of(reply)
        if files:
            await inter.followup.send(reply.get("text", ""), files=files)
        else:
            await inter.followup.send(reply.get("text", "no reply"))

    class Confirm(discord.ui.View):
        def __init__(self, user_id: int, payload: dict):
            super().__init__(timeout=C.PLACE_CONFIRM_SECONDS)
            self.user_id, self.payload, self.message, self.used = user_id, payload, None, False

        async def interaction_check(self, inter: discord.Interaction) -> bool:
            if inter.user.id != self.user_id:
                await inter.response.send_message("Only the person who typed /place can confirm it.", ephemeral=True)
                return False
            if self.used:                                          # a second click while the first is being handled
                await inter.response.send_message("Already handled — one order per /place.", ephemeral=True)
                return False
            self.used = True                                       # set before any await: clicks cannot race
            return True

        async def finish(self, inter, text):
            for item in self.children:
                item.disabled = True
            self.stop()
            await inter.edit_original_response(content=text, view=self)

        @discord.ui.button(label="Confirm order", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, _button):
            await inter.response.defer()
            reply = await ask("place", self.payload)
            await self.finish(inter, reply.get("text", "no reply"))

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, _button):
            await inter.response.defer()
            await self.finish(inter, "Cancelled — nothing was placed.")

        async def on_timeout(self):
            self.used = True
            for item in self.children:
                item.disabled = True
            if self.message:
                try:
                    await self.message.edit(content="⌛ Expired — nothing was placed.", view=self)
                except discord.HTTPException:
                    pass

    @tree.command(name="place", description="Place a market order with stop-loss and take-profit (you confirm before it is sent)")
    @app_commands.describe(side="Buy or sell", lot="Lot size, e.g. 0.5",
                           sl="Stop-loss: distance from entry (20 = $20 away) or an exact price (4095.5)",
                           tp="Take-profit: distance from entry (50 = $50 away) or an exact price (4165.5)",
                           filling="Order filling type (Auto = whatever your broker supports)")
    @app_commands.choices(side=[app_commands.Choice(name="Buy", value="buy"), app_commands.Choice(name="Sell", value="sell")],
                          filling=[app_commands.Choice(name="Auto (broker default)", value="auto"),
                                   app_commands.Choice(name="FOK — fill or kill", value="fok"),
                                   app_commands.Choice(name="IOC — immediate or cancel", value="ioc"),
                                   app_commands.Choice(name="Return", value="return")])
    async def place_cmd(inter: discord.Interaction, side: app_commands.Choice[str],
                        lot: app_commands.Range[float, 0.01, 1000.0], sl: app_commands.Range[float, 0.01, None],
                        tp: app_commands.Range[float, 0.01, None], filling: app_commands.Choice[str] | None = None):
        if not await allowed(inter, trade=True):
            return
        await inter.response.defer()
        payload = {"side": side.value, "lot": lot, "sl": sl, "tp": tp, "filling": filling.value if filling else "auto",
                   "user": inter.user.id}
        quote = await ask("quote", payload)
        if not quote.get("ok"):
            await inter.followup.send(quote.get("text", "no reply")); return
        view = Confirm(inter.user.id, payload)
        view.message = await inter.followup.send(f"**Confirm within {C.PLACE_CONFIRM_SECONDS} s**\n{quote['text']}", view=view, wait=True)

    @tree.command(name="alert", description="Notify me when price reaches a level")
    async def alert_cmd(inter: discord.Interaction, price: float):
        if await allowed(inter):
            await inter.response.defer(); await inter.followup.send(await run_text(f"add {price}"))

    @tree.command(name="alert_remove", description="Remove a price alert")
    async def alert_remove_cmd(inter: discord.Interaction, price: float):
        if await allowed(inter):
            await inter.response.defer(); await inter.followup.send(await run_text(f"remove {price}"))

    @tree.command(name="alerts", description="List pending price alerts")
    async def alerts_cmd(inter: discord.Interaction):
        if await allowed(inter):
            await inter.response.defer(); await inter.followup.send(await run_text("list"))

    @client.event
    async def on_ready():
        for guild in client.guilds:                                # per-server sync = commands appear immediately
            try:
                tree.copy_global_to(guild=guild); await tree.sync(guild=guild)
            except discord.HTTPException as e:
                log.error("Could not register slash commands in %s: %s (re-invite the bot with the "
                          "'applications.commands' scope)", guild.name, e)
        log.info("Discord ready as %s — /chart /place /alert /alerts", client.user)

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

    client.tree, client.Confirm = tree, Confirm
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
