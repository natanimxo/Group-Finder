#!/usr/bin/env python3
"""
Group Finder Bot
================

Your own private Telegram bot. Send it keywords from your phone; it searches
for groups (via @ai_crawlerbot and, optionally, Telegram's own search), keeps
the ones that allow text and charge no Stars, and lets you join them with one
tap — or joins them automatically.

Two Telegram connections run here:
  * the BOT (token from @BotFather)  -> the chat you talk to
  * YOUR ACCOUNT (logged in via /login) -> searches, checks and joins groups,
    because only a real account can join groups for you.

Designed for Railway: settings come from environment variables, data is kept
in the Railway volume (RAILWAY_VOLUME_MOUNT_PATH) so nothing is lost on restart.
"""

import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import Button, TelegramClient, errors, events, functions, types
from telethon.sessions import StringSession

import tg_group_finder as core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finder")
logging.getLogger("telethon").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Configuration (Railway "Variables" tab)
# --------------------------------------------------------------------------

def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if isinstance(v, str) and v.strip() else default


API_ID = env("API_ID") or env("TG_API_ID")
API_HASH = env("API_HASH") or env("TG_API_HASH")
BOT_TOKEN = env("BOT_TOKEN")
ACCESS_CODE = env("ACCESS_CODE")
OWNER_ID = env("OWNER_ID")
SEARCH_BOT = (env("SEARCH_BOT", "ai_crawlerbot") or "").lstrip("@")
BOT_TEMPLATE = env("BOT_TEMPLATE", "{kw}")
DATA_DIR = Path(env("DATA_DIR") or env("RAILWAY_VOLUME_MOUNT_PATH")
                or ("/data" if Path("/data").is_dir() else "data"))
JOIN_DELAY_MIN = int(env("JOIN_DELAY_MIN", "45"))
JOIN_DELAY_MAX = int(env("JOIN_DELAY_MAX", "120"))
RECHECK_DAYS = float(env("RECHECK_DAYS", "7"))
MAX_SHOWN = 40          # groups listed in chat per search (all of them go in the CSV)
CHUNK = 8               # groups per message (keeps button data under Telegram's 64-byte limit)

DEFAULT_SETTINGS = {
    "autojoin": False,
    "global": False,
    "no_approval": False,
    "match_keyword": False,
    "mute": True,
    "min_members": 0,
    "daily_joins": 20,
    "schedule_hours": 24,
    "pages": 5,
}
MIN_MEMBER_STEPS = [0, 50, 100, 500, 1000, 5000]
SCHEDULE_STEPS = [0, 6, 12, 24, 48]
JOIN_STEPS = [5, 10, 20, 30, 40]


# --------------------------------------------------------------------------
# Persistent storage (one JSON file in the volume)
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.d = {
            "owner_id": None, "session": None, "keywords": [], "settings": {},
            "groups": {}, "queue": [], "joins": {}, "last_auto_run": 0,
            "next_sid": 1, "queue_paused": False,
        }
        if path.exists():
            try:
                self.d.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:
                log.error("Could not read %s (%s); starting fresh", path, e)
        for k, v in DEFAULT_SETTINGS.items():
            self.d["settings"].setdefault(k, v)
        self.by_sid = {g["sid"]: k for k, g in self.d["groups"].items() if "sid" in g}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def s(self):
        return self.d["settings"]

    def group(self, key, create=False):
        g = self.d["groups"].get(key)
        if g is None and create:
            g = {"sid": self.d["next_sid"], "keywords": []}
            self.d["next_sid"] += 1
            self.d["groups"][key] = g
            self.by_sid[g["sid"]] = key
        return g

    def joins_today(self):
        return self.d["joins"].get(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0)

    def count_join(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.d["joins"] = {day: self.d["joins"].get(day, 0) + 1}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s or ""), quote=False)


def split_keywords(text):
    return [k.strip() for k in re.split(r"[\n,;]+", text or "") if k.strip()]


def fmt_members(n):
    return f"{n:,}" if isinstance(n, int) else "?"


def onoff(v):
    return "ON ✅" if v else "OFF"


class Progress:
    """One status message that gets edited, at most every few seconds."""

    def __init__(self, bot, chat, msg):
        self.bot, self.chat, self.msg, self.last, self.text = bot, chat, msg, 0, ""

    async def set(self, text, force=False):
        if self.msg is None:
            return
        if text == self.text or (not force and time.monotonic() - self.last < 4):
            return
        self.text, self.last = text, time.monotonic()
        try:
            await self.bot.edit_message(self.chat, self.msg, text, parse_mode="html")
        except Exception:
            pass


HELP = (
    "<b>🔎 Group Finder</b>\n\n"
    "<b>Search:</b> just send me keywords, one per line or separated by commas.\n"
    "I'll reply with groups that <b>allow text</b> and <b>don't charge Stars</b>, "
    "each with a ➕ Join button.\n\n"
    "<b>Commands</b>\n"
    "/login – connect your Telegram account (needed once)\n"
    "/watch word1, word2 – save keywords for the automatic daily search\n"
    "/list – show saved keywords · /unwatch word – remove one\n"
    "/run – search all saved keywords now\n"
    "/settings – auto-join, filters, daily limits\n"
    "/queue – groups waiting to be joined\n"
    "/groups – download every group found so far (CSV)\n"
    "/cancel – stop the current search or login\n"
    "/logout – disconnect your account"
)


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

class App:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(DATA_DIR / "state.json")
        if OWNER_ID:
            self.store.d["owner_id"] = int(OWNER_ID)
        self.bot = TelegramClient(str(DATA_DIR / "bot"), int(API_ID), API_HASH)
        self.user = None
        self.login = None                # login conversation state
        self.search_lock = asyncio.Lock()
        self.search_task = None
        self.join_wake = asyncio.Event()
        self.limit_notified_day = None

    # ---- basics ---------------------------------------------------------
    @property
    def owner(self):
        return self.store.d["owner_id"]

    async def say(self, text, **kw):
        if self.owner:
            kw.setdefault("parse_mode", "html")
            kw.setdefault("link_preview", False)
            return await self.bot.send_message(self.owner, text, **kw)

    async def user_ready(self):
        try:
            return self.user is not None and await self.user.is_user_authorized()
        except Exception:
            return False

    def opts(self):
        s = self.store.s
        return SimpleNamespace(
            bot=SEARCH_BOT, template=BOT_TEMPLATE, pages=s["pages"], bot_wait=25, bot_idle=4,
            global_limit=50, max_flood_wait=300, include_unverified=False,
            min_members=s["min_members"], max_slowmode=None, no_approval=s["no_approval"],
            match_keyword=s["match_keyword"], delay=2.5,
        )

    # ---- startup --------------------------------------------------------
    async def start(self):
        await self.bot.start(bot_token=BOT_TOKEN)
        me = await self.bot.get_me()
        log.info("Bot running as @%s", me.username)

        if self.store.d["session"]:
            self.user = TelegramClient(StringSession(self.store.d["session"]), int(API_ID), API_HASH)
            await self.user.connect()
            if not await self.user.is_user_authorized():
                log.warning("Saved login is no longer valid")
                self.user = None
                self.store.d["session"] = None
                self.store.save()
                await self.say("⚠️ Your Telegram login expired or was removed. Send /login to reconnect.")

        self.bot.add_event_handler(self.on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self.bot.add_event_handler(self.on_button, events.CallbackQuery())
        try:
            await self.bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="", commands=[
                    types.BotCommand("run", "Search all saved keywords now"),
                    types.BotCommand("watch", "Save keywords for the daily search"),
                    types.BotCommand("list", "Show saved keywords"),
                    types.BotCommand("settings", "Auto-join, filters, limits"),
                    types.BotCommand("queue", "Groups waiting to be joined"),
                    types.BotCommand("groups", "Download all groups found (CSV)"),
                    types.BotCommand("login", "Connect your Telegram account"),
                    types.BotCommand("cancel", "Stop the current search or login"),
                    types.BotCommand("help", "How to use this bot"),
                ]))
        except Exception as e:
            log.warning("Could not set command menu: %s", e)

        asyncio.create_task(self.join_worker())
        asyncio.create_task(self.scheduler())
        await self.bot.run_until_disconnected()

    # ---- incoming messages ----------------------------------------------
    async def on_message(self, event):
        uid = event.sender_id
        text = (event.raw_text or "").strip()

        if self.owner is None:
            parts = text.split(maxsplit=1)
            if not ACCESS_CODE:
                await event.reply("Set the ACCESS_CODE variable on Railway first, then send /start <code>.")
            elif parts[:1] == ["/start"] and parts[1:] == [ACCESS_CODE]:
                self.store.d["owner_id"] = uid
                self.store.save()
                await event.reply("🔐 This bot is now yours — it will ignore everyone else.\n\n" + HELP,
                                  parse_mode="html")
                if not await self.user_ready():
                    await self.say("Next step: send /login to connect your Telegram account.")
            else:
                await event.reply("This is a private bot. Send /start followed by your access code.")
            return
        if uid != self.owner:
            return  # silently ignore strangers

        if self.login is not None:
            return await self.login_step(event, text)

        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()

        if cmd in ("/start", "/help"):
            await event.reply(HELP, parse_mode="html")
        elif cmd == "/login":
            await self.begin_login(event)
        elif cmd == "/logout":
            await self.logout(event)
        elif cmd == "/cancel":
            if self.search_task and not self.search_task.done():
                self.search_task.cancel()
                await event.reply("⏹ Search stopped.")
            else:
                await event.reply("Nothing to cancel.")
        elif cmd == "/watch":
            await self.watch(event, arg)
        elif cmd == "/unwatch":
            await self.unwatch(event, arg)
        elif cmd == "/list":
            await self.show_keywords(event)
        elif cmd == "/run":
            kws = self.store.d["keywords"]
            if not kws:
                await event.reply("No saved keywords yet. Add some with /watch word1, word2")
            else:
                self.store.d["last_auto_run"] = time.time()
                self.store.save()
                self.launch_search(kws, only_new=False)
        elif cmd == "/settings":
            await self.show_settings()
        elif cmd == "/queue":
            await self.show_queue()
        elif cmd == "/groups":
            await self.export_groups()
        elif cmd == "/search":
            self.launch_search(split_keywords(arg), only_new=False)
        elif cmd.startswith("/"):
            await event.reply("Unknown command. Send /help to see what I can do.")
        else:
            kws = split_keywords(text)
            if kws:
                self.launch_search(kws, only_new=False)

    # ---- login (done inside the chat, no terminal) -----------------------
    async def begin_login(self, event):
        if await self.user_ready():
            me = await self.user.get_me()
            return await event.reply(f"Already connected as {me.first_name}. Use /logout to switch accounts.")
        self.login = {"step": "phone"}
        await event.reply(
            "📱 Send your phone number with country code, e.g. <code>+251911234567</code>\n\n"
            "This logs this bot into <b>your</b> account so it can search and join groups for you. "
            "Send /cancel to stop.", parse_mode="html")

    async def login_step(self, event, text):
        st = self.login
        if text.lower() in ("/cancel", "cancel"):
            if st.get("client"):
                await st["client"].disconnect()
            self.login = None
            return await event.reply("Login cancelled.")
        try:
            if st["step"] == "phone":
                phone = re.sub(r"[^\d+]", "", text)
                client = TelegramClient(StringSession(), int(API_ID), API_HASH)
                await client.connect()
                sent = await client.send_code_request(phone)
                st.update(step="code", phone=phone, hash=sent.phone_code_hash, client=client)
                await event.reply(
                    "✉️ Telegram just sent you a login code (look in your Telegram app, chat from \"Telegram\").\n\n"
                    "Send it here <b>with spaces between the digits</b>, like <code>1 2 3 4 5</code>.\n"
                    "⚠️ If you send it as one number, Telegram notices and blocks the login.",
                    parse_mode="html")
            elif st["step"] == "code":
                code = re.sub(r"\D", "", text)
                await self._delete(event)
                try:
                    await st["client"].sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
                except errors.SessionPasswordNeededError:
                    st["step"] = "password"
                    return await event.respond(
                        "🔑 Your account has two-step verification. Send your cloud password "
                        "(I'll delete the message right away).")
                await self.finish_login()
            elif st["step"] == "password":
                await self._delete(event)
                await st["client"].sign_in(password=text)
                await self.finish_login()
        except errors.PhoneNumberInvalidError:
            await event.respond("That phone number isn't valid. Send it again with the country code, e.g. +251…")
        except errors.PhoneCodeInvalidError:
            await event.respond("Wrong code. Send it again, with spaces between the digits.")
        except errors.PhoneCodeExpiredError:
            await self._abort_login(event, "That code expired (or Telegram blocked it because it was sent "
                                           "without spaces). Send /login to try again.")
        except errors.PasswordHashInvalidError:
            await event.respond("Wrong password. Try again, or /cancel.")
        except errors.FloodWaitError as e:
            await self._abort_login(event, f"Telegram says too many attempts — wait {e.seconds // 60 + 1} min, then /login.")
        except Exception as e:
            log.exception("login failed")
            await self._abort_login(event, f"Login failed: {e}. Send /login to try again.")

    async def _abort_login(self, event, msg):
        if self.login and self.login.get("client"):
            await self.login["client"].disconnect()
        self.login = None
        await event.respond(msg)

    async def _delete(self, event):
        try:
            await event.delete()
        except Exception:
            pass

    async def finish_login(self):
        client = self.login["client"]
        self.login = None
        if self.user:
            await self.user.disconnect()
        self.user = client
        self.store.d["session"] = client.session.save()
        self.store.save()
        me = await client.get_me()
        try:  # make sure the search bot accepts messages from this account
            await client.send_message(SEARCH_BOT, "/start")
        except Exception as e:
            log.warning("could not /start @%s: %s", SEARCH_BOT, e)
        await self.say(
            f"✅ Connected as <b>{esc(me.first_name)}</b>.\n\n"
            "Now send me some keywords to search, or save them with /watch for the daily automatic search.")
        self.join_wake.set()

    async def logout(self, event):
        if self.user:
            try:
                await self.user.log_out()
            except Exception:
                pass
        self.user = None
        self.store.d["session"] = None
        self.store.save()
        await event.reply("Logged out. Send /login to connect again.")

    # ---- keywords ---------------------------------------------------------
    async def watch(self, event, arg):
        new = [k for k in split_keywords(arg)
               if k.lower() not in {x.lower() for x in self.store.d["keywords"]}]
        if not arg:
            return await event.reply("Usage: /watch crypto chat, forex traders")
        self.store.d["keywords"].extend(new)
        if not self.store.d["last_auto_run"]:
            self.store.d["last_auto_run"] = time.time()
        self.store.save()
        hrs = self.store.s["schedule_hours"]
        when = f"every {hrs} h" if hrs else "only when you send /run (daily search is off in /settings)"
        await event.reply(
            f"Saved {len(new)} keyword(s). You now have {len(self.store.d['keywords'])}.\n"
            f"I'll search them {when} and tell you about new groups.",
            buttons=[Button.inline("🔎 Search them now", b"run")])

    async def unwatch(self, event, arg):
        rm = {k.lower() for k in split_keywords(arg)}
        before = len(self.store.d["keywords"])
        self.store.d["keywords"] = [k for k in self.store.d["keywords"] if k.lower() not in rm]
        self.store.save()
        await event.reply(f"Removed {before - len(self.store.d['keywords'])}. /list to see what's left.")

    async def show_keywords(self, event):
        kws = self.store.d["keywords"]
        if not kws:
            return await event.reply("No saved keywords. Add with /watch word1, word2")
        await event.reply("📝 <b>Saved keywords</b>\n" + "\n".join(f"• {esc(k)}" for k in kws)
                          + "\n\nRemove one with /unwatch word", parse_mode="html",
                          buttons=[Button.inline("🔎 Search them now", b"run")])

    # ---- searching ----------------------------------------------------------
    def launch_search(self, keywords, only_new):
        if not keywords:
            return
        self.search_task = asyncio.create_task(self.run_search(keywords, only_new))

    async def run_search(self, keywords, only_new, automatic=False):
        if not await self.user_ready():
            return await self.say("First connect your Telegram account: send /login")
        if self.search_lock.locked():
            if not automatic:
                await self.say("A search is already running — I'll finish that one first. (/cancel to stop it)")
            return
        async with self.search_lock:
            try:
                await self._search(keywords, only_new, automatic)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("search failed")
                await self.say(f"⚠️ Search hit an error: {esc(e)}")

    async def _search(self, keywords, only_new, automatic):
        st = self.store
        opts = self.opts()
        title = "⏰ Daily search" if automatic else "🔎 Searching"
        # the daily run works silently and only speaks up when it finds something new
        msg = None if automatic else await self.say(f"{title}: {len(keywords)} keyword(s)…")
        prog = Progress(self.bot, self.owner, msg)

        search_bot = None
        try:
            search_bot = await self.user.get_entity(SEARCH_BOT)
        except Exception as e:
            await self.say(f"Can't open @{esc(SEARCH_BOT)} ({esc(e)}). Using Telegram's own search instead.")
        use_global = st.s["global"] or search_bot is None

        # 1) collect candidates
        pending = {}
        for i, kw in enumerate(keywords, 1):
            await prog.set(f"{title}: <b>{esc(kw)}</b> ({i}/{len(keywords)}) · {len(pending)} links so far")
            if search_bot is not None:
                try:
                    for kind, val in await core.search_bot(self.user, search_bot, kw, opts, log.info):
                        p = pending.setdefault(f"{kind}:{val}", {"kind": kind, "value": val, "kw": set(),
                                                                 "ent": None, "via": set()})
                        p["kw"].add(kw)
                        p["via"].add("bot")
                except errors.FloodWaitError as e:
                    log.warning("bot search flood %s", e.seconds)
                await asyncio.sleep(opts.delay)
            if use_global:
                try:
                    for ch in await core.global_search(self.user, kw, opts, log.info):
                        u = (ch.username or "").lower()
                        key = f"username:{u}" if u else f"id:{ch.id}"
                        p = pending.setdefault(key, {"kind": "entity", "value": u, "kw": set(),
                                                     "ent": None, "via": set()})
                        p["kw"].add(kw)
                        p["via"].add("telegram search")
                        p["ent"] = ch
                except core.FloodTooLong:
                    pass
                await asyncio.sleep(opts.delay)

        # 2) check each group
        now = time.time()
        rows, stopped, checked = [], False, 0
        total = len(pending)
        for key, p in pending.items():
            checked += 1
            await prog.set(f"{title}: checking groups {checked}/{total}…")
            g = st.group(key)
            is_new = g is None or "status" not in g
            fresh = g is not None and now - g.get("_checked", 0) < RECHECK_DAYS * 86400
            if not fresh:
                if stopped:
                    continue
                try:
                    if p["ent"] is not None:
                        r = await core.inspect_channel(self.user, p["ent"], opts, log.info)
                    elif p["kind"] == "invite":
                        r = await core.inspect_invite(self.user, p["value"], opts, log.info)
                    else:
                        r = await core.inspect_username(self.user, p["value"], opts, log.info)
                except core.FloodTooLong as e:
                    stopped = e.seconds
                    continue
                g = st.group(key, create=True)
                g.update({k: v for k, v in r.items() if not k.startswith("_")})
                g["_checked"] = now
                g.setdefault("first_seen", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                if p["kind"] == "invite":
                    g["invite"] = p["value"]
                await asyncio.sleep(opts.delay)
            g["keywords"] = sorted(set(g.get("keywords", [])) | p["kw"])
            g["found_via"] = " + ".join(sorted(set(filter(None, g.get("found_via", "").split(" + "))) | p["via"]))
            g["_new"] = is_new
            rows.append((key, g))
        st.save()

        good = [(k, g) for k, g in rows if core.passes(g, opts, set(g["keywords"]))]
        good.sort(key=lambda kg: (not kg[1].get("_new"), -(kg[1].get("members") or 0)))
        new_good = [(k, g) for k, g in good if g.get("_new")]
        shown = new_good if only_new else good

        head = (f"✅ <b>Done.</b> Checked {total} links → <b>{len(good)}</b> groups allow text with no Stars fee "
                f"(<b>{len(new_good)}</b> new).")
        if stopped:
            head += (f"\n⏳ Telegram asked me to slow down ({stopped // 60 + 1} min), so some links weren't "
                     "checked yet — they'll be checked next run.")
        if automatic:
            if not shown:
                return  # nothing new: stay quiet
            await self.say(head)
        else:
            await prog.set(head, force=True)

        if not shown:
            return await self.say("No new groups this time." if only_new else
                                  "No groups passed the filters. Try other keywords or loosen /settings.")
        await self.send_group_list(shown, f"{'⏰ ' if automatic else ''}🆕 New groups" if only_new else "📋 Groups")

        if st.s["autojoin"]:
            added = self.enqueue([k for k, g in shown if not g.get("joined")])
            if added:
                await self.say(f"🤖 Auto-join is on: {added} group(s) added to the join queue. /queue to see it.")

    async def send_group_list(self, items, title):
        listed = items[:MAX_SHOWN]
        await self.say(f"<b>{title}</b> ({len(items)})")
        for i in range(0, len(listed), CHUNK):
            chunk = listed[i:i + CHUNK]
            lines, buttons, joinable = [], [], []
            for key, g in chunk:
                tags = []
                if g.get("_new"):
                    tags.append("🆕")
                if g.get("join_approval"):
                    tags.append("needs approval")
                if g.get("slowmode_s"):
                    tags.append(f"slow mode {g['slowmode_s']}s")
                if g.get("joined"):
                    tags.append(f"✔️ {g['joined']}")
                lines.append(f"• <b>{esc(g.get('title'))}</b> — {fmt_members(g.get('members'))} members"
                             f"{' · ' + ' · '.join(tags) if tags else ''}\n   {esc(g.get('link'))}")
                if not g.get("joined"):
                    joinable.append(g["sid"])
                    buttons.append([Button.inline(f"➕ Join {(g.get('title') or '')[:30]}", f"j|{g['sid']}".encode())])
            if len(joinable) > 1:
                buttons.append([Button.inline(f"➕ Join all {len(joinable)} above",
                                              ("ja|" + ",".join(map(str, joinable))).encode())])
            await self.say("\n".join(lines), buttons=buttons or None)
        if len(items) > MAX_SHOWN or len(items) > CHUNK:
            await self.send_csv([g for _, g in items], "groups_found.csv",
                                f"All {len(items)} groups from this search")

    # ---- CSV export -------------------------------------------------------
    async def send_csv(self, groups, name, caption):
        cols = core.COLUMNS + ["joined"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for g in groups:
            w.writerow({**g, "keywords": " | ".join(g.get("keywords", []))})
        data = io.BytesIO(("﻿" + buf.getvalue()).encode("utf-8"))
        data.name = name
        await self.bot.send_file(self.owner, data, caption=caption, force_document=True)

    async def export_groups(self):
        opts = self.opts()
        gs = [g for g in self.store.d["groups"].values()
              if g.get("joined") or core.passes(g, opts, set(g.get("keywords", [])))]
        if not gs:
            return await self.say("No groups found yet. Send me some keywords first.")
        gs.sort(key=lambda g: -(g.get("members") or 0))
        await self.send_csv(gs, "all_groups.csv", f"{len(gs)} groups (open in Excel or Google Sheets)")

    # ---- buttons ------------------------------------------------------------
    async def on_button(self, event):
        if event.sender_id != self.owner:
            return await event.answer("Not your bot.", alert=True)
        data = event.data.decode(errors="ignore")
        s = self.store.s
        if data.startswith("j|"):
            n = self.enqueue_sids([data[2:]])
            await event.answer("Added to the join queue ✅" if n else "Already joined or queued")
        elif data.startswith("ja|"):
            n = self.enqueue_sids(data[3:].split(","))
            await event.answer(f"{n} group(s) added to the join queue ✅" if n else "Already joined or queued")
        elif data == "run":
            await event.answer()
            self.store.d["last_auto_run"] = time.time()
            self.launch_search(self.store.d["keywords"], only_new=False)
        elif data.startswith("set|"):
            name = data[4:]
            if name in ("autojoin", "global", "no_approval", "match_keyword", "mute"):
                s[name] = not s[name]
            elif name == "min_members":
                s[name] = self._next(MIN_MEMBER_STEPS, s[name])
            elif name == "schedule_hours":
                s[name] = self._next(SCHEDULE_STEPS, s[name])
            elif name == "daily_joins":
                s[name] = self._next(JOIN_STEPS, s[name])
            self.store.save()
            await event.answer("Saved")
            await event.edit(self.settings_text(), buttons=self.settings_buttons(), parse_mode="html")
        elif data == "q|clear":
            self.store.d["queue"] = []
            self.store.save()
            await event.answer("Queue cleared")
            await event.edit("Join queue cleared.")
        elif data == "q|resume":
            self.store.d["queue_paused"] = False
            self.store.save()
            self.join_wake.set()
            await event.answer("Resumed")
            await event.edit("▶️ Join queue resumed.")
        else:
            await event.answer()

    @staticmethod
    def _next(steps, cur):
        return steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else steps[0]

    # ---- settings -----------------------------------------------------------
    def settings_text(self):
        s = self.store.s
        sched = f"every {s['schedule_hours']} h" if s["schedule_hours"] else "OFF"
        return (
            "⚙️ <b>Settings</b> (tap a button to change)\n\n"
            f"Auto-join groups found: <b>{onoff(s['autojoin'])}</b>\n"
            f"Max joins per day: <b>{s['daily_joins']}</b>\n"
            f"Mute groups after joining: <b>{onoff(s['mute'])}</b>\n"
            f"Also use Telegram's own search: <b>{onoff(s['global'])}</b>\n"
            f"Skip groups needing admin approval: <b>{onoff(s['no_approval'])}</b>\n"
            f"Only groups whose name/description has the keyword: <b>{onoff(s['match_keyword'])}</b>\n"
            f"Minimum members: <b>{s['min_members']}</b>\n"
            f"Automatic search of saved keywords: <b>{sched}</b>"
        )

    def settings_buttons(self):
        s = self.store.s
        b = lambda label, key: Button.inline(label, f"set|{key}".encode())
        return [
            [b(f"Auto-join: {onoff(s['autojoin'])}", "autojoin"), b(f"Joins/day: {s['daily_joins']}", "daily_joins")],
            [b(f"Mute after join: {onoff(s['mute'])}", "mute"), b(f"Telegram search: {onoff(s['global'])}", "global")],
            [b(f"Skip approval: {onoff(s['no_approval'])}", "no_approval"),
             b(f"Keyword in name: {onoff(s['match_keyword'])}", "match_keyword")],
            [b(f"Min members: {s['min_members']}", "min_members"),
             b(f"Auto search: {str(s['schedule_hours']) + 'h' if s['schedule_hours'] else 'OFF'}", "schedule_hours")],
        ]

    async def show_settings(self):
        await self.say(self.settings_text(), buttons=self.settings_buttons())

    # ---- join queue -----------------------------------------------------------
    def enqueue_sids(self, sids):
        keys = []
        for sid in sids:
            try:
                k = self.store.by_sid.get(int(sid))
            except ValueError:
                k = None
            if k:
                keys.append(k)
        return self.enqueue(keys)

    def enqueue(self, keys):
        q = self.store.d["queue"]
        added = 0
        for k in keys:
            g = self.store.group(k)
            if g and not g.get("joined") and k not in q:
                q.append(k)
                added += 1
        if added:
            self.store.save()
            self.join_wake.set()
        return added

    async def show_queue(self):
        q = self.store.d["queue"]
        s = self.store.s
        head = (f"🧾 <b>Join queue</b>: {len(q)} waiting · joined today {self.store.joins_today()}/{s['daily_joins']}"
                f"{' · ⏸ paused' if self.store.d['queue_paused'] else ''}")
        if not q:
            return await self.say(head + "\n\nEmpty. Tap ➕ Join on any search result to add groups.")
        lines = [f"• {esc(self.store.group(k).get('title') or k)}" for k in q[:30]]
        more = f"\n…and {len(q) - 30} more" if len(q) > 30 else ""
        btns = [[Button.inline("🗑 Clear queue", b"q|clear")]]
        if self.store.d["queue_paused"]:
            btns.insert(0, [Button.inline("▶️ Resume", b"q|resume")])
        await self.say(head + "\n\n" + "\n".join(lines) + more +
                       f"\n\nI join one group every {JOIN_DELAY_MIN}–{JOIN_DELAY_MAX} s to stay within Telegram's limits.",
                       buttons=btns)

    async def join_worker(self):
        while True:
            try:
                await self._join_tick()
            except Exception:
                log.exception("join worker error")
                await asyncio.sleep(30)

    async def _join_tick(self):
        st = self.store
        q = st.d["queue"]
        if not q or st.d["queue_paused"] or not await self.user_ready():
            self.join_wake.clear()
            try:
                await asyncio.wait_for(self.join_wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            return

        limit = st.s["daily_joins"]
        if st.joins_today() >= limit:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self.limit_notified_day != today:
                self.limit_notified_day = today
                await self.say(f"⏸ Reached today's limit of {limit} joins. The other {len(q)} will be joined "
                               "tomorrow (you can change the limit in /settings).")
            await asyncio.sleep(300)
            return

        key = q[0]
        g = st.group(key) or {}
        try:
            result = await self.join_one(key, g)
        except errors.FloodWaitError as e:
            mins = e.seconds // 60 + 1
            await self.say(f"⏳ Telegram asked me to pause joining for {mins} min. I'll continue automatically.")
            await asyncio.sleep(e.seconds + 5)
            return
        except errors.ChannelsTooMuchError:
            st.d["queue_paused"] = True
            st.save()
            await self.say("⛔ Your account is in the maximum number of groups/channels Telegram allows "
                           "(500, or 1000 with Premium). Leave some, then open /queue and tap Resume.")
            return

        q.pop(0)
        g["joined"] = result
        if result in ("joined", "request sent"):
            st.count_join()
        st.save()
        icon = {"joined": "✅", "request sent": "📨", "already a member": "✔️"}.get(result, "⚠️")
        verb = {"joined": "Joined", "request sent": "Join request sent to",
                "already a member": "Already in"}.get(result, f"Couldn't join ({result}):")
        await self.say(f"{icon} {verb} <b>{esc(g.get('title'))}</b>  {esc(g.get('link'))}"
                       f"{f'  · {len(q)} left in queue' if q else ''}")
        if q:
            await asyncio.sleep(random.uniform(JOIN_DELAY_MIN, JOIN_DELAY_MAX))

    async def join_one(self, key, g):
        try:
            if key.startswith("invite:"):
                upd = await self.user(functions.messages.ImportChatInviteRequest(g.get("invite") or key[7:]))
                chat = (getattr(upd, "chats", None) or [None])[0]
            else:
                uname = g.get("username") or (key.split(":", 1)[1] if key.startswith("username:") else "")
                if not uname:
                    return "no public link"
                chat = await self.user.get_entity(uname)
                await self.user(functions.channels.JoinChannelRequest(chat))
        except errors.UserAlreadyParticipantError:
            return "already a member"
        except errors.InviteRequestSentError:
            return "request sent"
        except (errors.FloodWaitError, errors.ChannelsTooMuchError):
            raise
        except (errors.ChannelPrivateError, errors.InviteHashExpiredError, errors.InviteHashInvalidError,
                errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
            return "link no longer works"
        except errors.RPCError as e:
            return f"Telegram error {e.message}"

        if self.store.s["mute"] and chat is not None:
            try:
                peer = await self.user.get_input_entity(chat)
                await self.user(functions.account.UpdateNotifySettingsRequest(
                    peer=types.InputNotifyPeer(peer),
                    settings=types.InputPeerNotifySettings(mute_until=2**31 - 1)))
            except Exception as e:
                log.warning("mute failed: %s", e)
        return "joined"

    # ---- daily automatic search ------------------------------------------------
    async def scheduler(self):
        while True:
            await asyncio.sleep(60)
            try:
                st = self.store
                hrs = st.s["schedule_hours"]
                if not hrs or not st.d["keywords"] or not self.owner or self.search_lock.locked():
                    continue
                if time.time() - (st.d["last_auto_run"] or 0) < hrs * 3600:
                    continue
                if not await self.user_ready():
                    continue
                st.d["last_auto_run"] = time.time()
                st.save()
                await self.run_search(list(st.d["keywords"]), only_new=True, automatic=True)
            except Exception:
                log.exception("scheduler error")


def main():
    missing = [n for n, v in (("API_ID", API_ID), ("API_HASH", API_HASH), ("BOT_TOKEN", BOT_TOKEN)) if not v]
    if missing:
        raise SystemExit(f"Missing variables: {', '.join(missing)}. Add them in Railway → your service → Variables.")
    if env("RAILWAY_ENVIRONMENT") and DATA_DIR == Path("data"):
        log.warning("No Railway volume found! Add a volume mounted at /data, otherwise your login, "
                    "settings and found groups are lost every time Railway restarts the bot.")
    log.info("Saving data in %s", DATA_DIR.resolve())
    if not ACCESS_CODE and not OWNER_ID:
        log.warning("ACCESS_CODE is not set — nobody can claim the bot until you add it.")
    asyncio.run(App().start())


if __name__ == "__main__":
    main()
