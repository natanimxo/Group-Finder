#!/usr/bin/env python3
"""
Telegram Group Finder
=====================

Give it keywords -> it asks a search bot (default: @ai_crawlerbot) and,
optionally, Telegram's own global search -> it checks every group it finds
and keeps only the ones that are:

  * real groups (supergroups), not broadcast channels
  * open for members to send plain text messages
  * NOT charging Telegram Stars per message
  * not flagged as scam / fake

Results go to CSV files you can open in Excel or Google Sheets.
It only READS: it never joins groups and never posts in them.

Runs on your own Telegram account through Telegram's official API
(you need an api_id / api_hash from https://my.telegram.org).
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, functions, types

# --------------------------------------------------------------------------
# Link / mention parsing
# --------------------------------------------------------------------------

LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(\+[\w-]+|joinchat/[\w-]+|[A-Za-z][\w]{3,31})",
    re.I,
)
MENTION_RE = re.compile(r"(?<![\w/@.])@([A-Za-z][\w]{3,31})")

# Buttons that usually mean "next page of results"
NEXT_RE = re.compile(
    r"(next|more|»|›|▶|➡|→|⏩|⏭|>>|следующ|далее|ещ[её]|siguiente|suivant|weiter|próxima)",
    re.I,
)

# t.me paths that are never groups
RESERVED = {
    "joinchat", "addstickers", "addemoji", "addlist", "addtheme", "share",
    "proxy", "socks", "iv", "setlanguage", "login", "confirmphone", "boost",
    "invoice", "giftcode", "nft", "contact", "stars", "premium", "bg", "c",
    "s", "m", "telegram", "botfather",
}


def parse_link_target(g: str):
    """Turn the captured part of a t.me link into ('username'|'invite', value)."""
    if g.startswith("+"):
        return ("invite", g[1:])
    if g.lower().startswith("joinchat/"):
        return ("invite", g.split("/", 1)[1])
    return username_candidate(g)


def username_candidate(name: str):
    n = name.strip().strip("_")
    low = n.lower()
    if not n or low in RESERVED or low.endswith("bot"):
        return None
    return ("username", low)


def _btn_url(b):
    """URL of a button (works with old and new Telethon button layouts)."""
    return getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None)


def _btn_is_callback(b):
    names = type(b).__name__ + type(getattr(b, "type", None)).__name__
    return "Callback" in names


def extract_candidates(msg, skip_usernames=()):
    """Pull every group-looking link/mention out of a bot message
    (plain text, hidden text links, and URL buttons)."""
    found = set()
    texts = []
    raw = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or ""
    texts.append(raw)

    for ent in getattr(msg, "entities", None) or []:
        url = getattr(ent, "url", None)
        if url:
            texts.append(url)

    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for btn in getattr(row, "buttons", None) or []:
            url = _btn_url(btn)
            if url:
                texts.append(url)

    for t in texts:
        for m in LINK_RE.finditer(t):
            c = parse_link_target(m.group(1))
            if c:
                found.add(c)
    for m in MENTION_RE.finditer(raw):
        c = username_candidate(m.group(1))
        if c:
            found.add(c)

    skip = {s.lower().lstrip("@") for s in skip_usernames}
    return {c for c in found if not (c[0] == "username" and c[1] in skip)}


def find_next_button(msg):
    """Return (row, col) of a 'next page' callback button, or None."""
    markup = getattr(msg, "reply_markup", None)
    for i, row in enumerate(getattr(markup, "rows", None) or []):
        for j, btn in enumerate(getattr(row, "buttons", None) or []):
            if _btn_is_callback(btn) and NEXT_RE.search(btn.text or ""):
                return (i, j)
    return None


def msg_signature(msg):
    btns = []
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for b in getattr(row, "buttons", None) or []:
            btns.append((b.text, _btn_url(b)))
    return (msg.id, getattr(msg, "raw_text", ""), tuple(btns))


# --------------------------------------------------------------------------
# Flood-wait handling
# --------------------------------------------------------------------------

class FloodTooLong(Exception):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


async def safe_call(fn, max_wait, log):
    """Run fn(); if Telegram says 'slow down', wait (up to max_wait) and retry."""
    while True:
        try:
            return await fn()
        except errors.FloodWaitError as e:
            if e.seconds > max_wait:
                raise FloodTooLong(e.seconds)
            log(f"   ⏳ Telegram asked us to wait {e.seconds}s ...")
            await asyncio.sleep(e.seconds + 1)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

async def collect_bot_replies(client, bot, after_id, max_wait, idle):
    """Wait for the bot's replies to arrive; stop after `idle` quiet seconds."""
    got = {}
    start = last_new = time.monotonic()
    while True:
        await asyncio.sleep(1.0)
        batch = await client.get_messages(bot, min_id=after_id, limit=100)
        new = False
        for m in batch:
            if not m.out and m.id not in got:
                got[m.id] = m
                new = True
        now = time.monotonic()
        if new:
            last_new = now
        if (got and now - last_new >= idle) or now - start >= max_wait:
            break
    return [got[k] for k in sorted(got)]


async def search_bot(client, bot, keyword, args, log):
    """Send one keyword to the search bot and harvest every page of results."""
    text = args.template.format(kw=keyword)
    sent = await client.send_message(bot, text)
    msgs = await collect_bot_replies(client, bot, sent.id, args.bot_wait, args.bot_idle)
    if not msgs:
        log(f"   (no reply from @{args.bot} — is the bot online / did you /start it?)")
        return set()

    candidates = set()
    seen_sigs = set()
    for m in msgs:
        candidates |= extract_candidates(m, skip_usernames=[args.bot])
        seen_sigs.add(msg_signature(m))

    pages = 1
    while pages < args.pages:
        target, pos = None, None
        for m in reversed(msgs):
            pos = find_next_button(m)
            if pos:
                target = m
                break
        if not target:
            break
        try:
            await target.click(pos[0], pos[1])
        except (errors.BotResponseTimeoutError, errors.MessageIdInvalidError,
                errors.DataInvalidError) as e:
            log(f"   (next-page button failed: {type(e).__name__})")
            break
        except Exception as e:  # bots answer callbacks in many odd ways
            log(f"   (next-page button: {e})")
        await asyncio.sleep(args.bot_idle)
        edited = await client.get_messages(bot, ids=target.id)
        newer = await collect_bot_replies(client, bot, max(x.id for x in msgs),
                                          args.bot_idle + 2, args.bot_idle)
        page_msgs = ([edited] if edited else []) + newer
        changed = False
        for m in page_msgs:
            sig = msg_signature(m)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            changed = True
            candidates |= extract_candidates(m, skip_usernames=[args.bot])
        if not changed:
            break
        msgs = [m for m in msgs if m.id != target.id] + page_msgs
        pages += 1

    log(f"   bot: {len(candidates)} links over {pages} page(s)")
    return candidates


async def global_search(client, keyword, args, log):
    """Telegram's built-in public search (same as typing in the search bar)."""
    res = await safe_call(
        lambda: client(functions.contacts.SearchRequest(q=keyword, limit=args.global_limit)),
        args.max_flood_wait, log,
    )
    chats = [c for c in res.chats if isinstance(c, types.Channel)]
    log(f"   global search: {len(chats)} public chats")
    return chats


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------

def link_for(ent=None, username=None, invite=None):
    u = username or (getattr(ent, "username", None) if ent else None)
    if not u and ent is not None:
        for un in getattr(ent, "usernames", None) or []:
            if getattr(un, "active", False):
                u = un.username
                break
    if u:
        return f"https://t.me/{u}"
    if invite:
        return f"https://t.me/+{invite}"
    return ""


async def inspect_channel(client, ent, args, log):
    """Decide whether a Channel object is a usable, free, text-allowed group."""
    r = {
        "title": ent.title,
        "username": (getattr(ent, "username", None) or "").lower(),
        "link": link_for(ent),
        "id": ent.id,
        "is_group": bool(ent.megagroup) and not getattr(ent, "gigagroup", False),
        "members": getattr(ent, "participants_count", None),
        "text_allowed": None,
        "paid_stars": int(getattr(ent, "send_paid_messages_stars", None) or 0),
        "slowmode_s": 0,
        "join_approval": bool(getattr(ent, "join_request", False)),
        "forum": bool(getattr(ent, "forum", False)),
        "scam_or_fake": bool(getattr(ent, "scam", False) or getattr(ent, "fake", False)),
        "about": "",
        "status": "",
    }
    if ent.broadcast and not ent.megagroup:
        r["status"] = "channel (broadcast only)"
        return r
    if getattr(ent, "gigagroup", False):
        r["status"] = "broadcast group (only admins post)"
        return r

    rights = getattr(ent, "default_banned_rights", None)
    if rights is not None:
        r["text_allowed"] = not (rights.send_messages or getattr(rights, "send_plain", False))

    try:
        full = await safe_call(
            lambda: client(functions.channels.GetFullChannelRequest(ent)),
            args.max_flood_wait, log,
        )
        fc = full.full_chat
        r["members"] = fc.participants_count or r["members"]
        r["slowmode_s"] = fc.slowmode_seconds or 0
        r["paid_stars"] = max(r["paid_stars"], int(getattr(fc, "send_paid_messages_stars", None) or 0))
        r["about"] = (fc.about or "").replace("\n", " ")[:300]
        # full chat sometimes carries a fresher copy of the channel object
        for c in full.chats:
            if c.id == ent.id and getattr(c, "default_banned_rights", None) is not None:
                br = c.default_banned_rights
                r["text_allowed"] = not (br.send_messages or getattr(br, "send_plain", False))
                r["paid_stars"] = max(r["paid_stars"], int(getattr(c, "send_paid_messages_stars", None) or 0))
    except FloodTooLong:
        raise
    except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
        r["status"] = f"private/inaccessible ({type(e).__name__})"
        return r

    r["status"] = "ok"
    return r


async def inspect_username(client, username, args, log):
    try:
        ent = await safe_call(lambda: client.get_entity(username), args.max_flood_wait, log)
    except FloodTooLong:
        raise
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        return {"username": username, "link": link_for(username=username), "status": "not found"}
    except Exception as e:
        return {"username": username, "link": link_for(username=username), "status": f"error: {e}"}
    if not isinstance(ent, types.Channel):
        kind = "user/bot" if isinstance(ent, types.User) else type(ent).__name__
        return {"username": username, "link": link_for(username=username), "status": f"not a group ({kind})"}
    return await inspect_channel(client, ent, args, log)


async def inspect_invite(client, h, args, log):
    base = {"link": link_for(invite=h), "username": "", "invite": h}
    try:
        inv = await safe_call(
            lambda: client(functions.messages.CheckChatInviteRequest(hash=h)),
            args.max_flood_wait, log,
        )
    except FloodTooLong:
        raise
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
        return {**base, "status": "invite expired/invalid"}
    except Exception as e:
        return {**base, "status": f"error: {e}"}

    chat = getattr(inv, "chat", None)
    if isinstance(chat, types.Channel):  # ChatInviteAlready / ChatInvitePeek
        r = await inspect_channel(client, chat, args, log)
        r["link"] = r["link"] or base["link"]
        return r
    if isinstance(inv, types.ChatInvite):
        is_group = bool(inv.megagroup) or not inv.broadcast
        return {
            **base,
            "title": inv.title,
            "is_group": is_group,
            "members": inv.participants_count,
            "join_approval": bool(inv.request_needed),
            "about": (getattr(inv, "about", "") or "").replace("\n", " ")[:300],
            "text_allowed": None,
            "paid_stars": None,
            "status": "private invite – permissions only visible after joining"
            if is_group else "channel (broadcast only)",
        }
    return {**base, "status": "unknown invite type"}


def passes(r, args, keyword_hits):
    """Your filter: a real group, text allowed, no Stars fee."""
    if r.get("status") not in ("ok",) and not (
        args.include_unverified and str(r.get("status", "")).startswith("private invite")
    ):
        return False
    if not r.get("is_group") or r.get("scam_or_fake"):
        return False
    if r.get("text_allowed") is False:
        return False
    if r.get("text_allowed") is None and not args.include_unverified:
        return False
    if (r.get("paid_stars") or 0) > 0:
        return False
    if (r.get("members") or 0) < args.min_members:
        return False
    if args.max_slowmode is not None and (r.get("slowmode_s") or 0) > args.max_slowmode:
        return False
    if args.no_approval and r.get("join_approval"):
        return False
    if args.match_keyword:
        hay = " ".join(str(r.get(k, "")) for k in ("title", "username", "about")).lower()
        if not any(k.lower() in hay for k in keyword_hits):
            return False
    return True


# --------------------------------------------------------------------------
# Storage (cache so groups aren't re-checked every run; "first seen" dates)
# --------------------------------------------------------------------------

def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p, data):
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


COLUMNS = ["title", "link", "members", "keywords", "found_via", "text_allowed",
           "paid_stars", "slowmode_s", "join_approval", "forum", "status",
           "first_seen", "about"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # -sig: opens cleanly in Excel
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_config(args):
    cfg = load_json(args.config, {}) if Path(args.config).exists() else {}
    api_id = os.environ.get("TG_API_ID") or cfg.get("api_id")
    api_hash = os.environ.get("TG_API_HASH") or cfg.get("api_hash")
    if not api_id or not api_hash:
        sys.exit(
            "Missing api_id / api_hash.\n"
            "1) Get them at https://my.telegram.org -> API development tools\n"
            f"2) Put them in {args.config}  (see config.example.json)\n"
            "   or set TG_API_ID and TG_API_HASH environment variables."
        )
    return int(api_id), str(api_hash)


def read_keywords(args):
    kws = list(args.keywords or [])
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                kws.append(line)
    seen, out = set(), []
    for k in kws:
        if k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    if not out:
        sys.exit("No keywords. Use -k word1 word2 ...  or  -f keywords.txt")
    return out


async def run(args):
    def log(s):
        print(s, flush=True)

    api_id, api_hash = load_config(args)
    keywords = read_keywords(args)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "cache.json"
    cache = load_json(cache_path, {})   # key -> result dict
    now = datetime.now(timezone.utc)
    recheck_after = args.recheck_days * 86400

    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()  # first run asks for phone number + login code
    me = await client.get_me()
    log(f"Logged in as {me.first_name} (@{me.username or '-'})\n")

    bot = None
    if not args.no_bot:
        try:
            bot = await client.get_entity(args.bot)
        except Exception as e:
            log(f"Can't open @{args.bot}: {e}. Continuing with global search only.")
            args.global_search = True

    # 1) Gather candidates per keyword ------------------------------------
    pending = {}    # key -> {"kind","value","keywords":set,"via":set,"entity":Channel|None}
    for kw in keywords:
        log(f"🔎 {kw}")
        if bot is not None:
            try:
                for kind, val in await search_bot(client, bot, kw, args, log):
                    key = f"{kind}:{val}"
                    p = pending.setdefault(key, {"kind": kind, "value": val, "keywords": set(),
                                                 "via": set(), "entity": None})
                    p["keywords"].add(kw)
                    p["via"].add("bot")
            except errors.FloodWaitError as e:
                log(f"   bot search paused by Telegram for {e.seconds}s; skipping")
            await asyncio.sleep(args.delay)
        if args.global_search:
            try:
                for ch in await global_search(client, kw, args, log):
                    u = (ch.username or "").lower()
                    key = f"username:{u}" if u else f"id:{ch.id}"
                    p = pending.setdefault(key, {"kind": "entity", "value": u or ch.id,
                                                 "keywords": set(), "via": set(), "entity": None})
                    p["keywords"].add(kw)
                    p["via"].add("global")
                    p["entity"] = ch
            except FloodTooLong as e:
                log(f"   global search rate-limited for {e.seconds}s; skipping")
            await asyncio.sleep(args.delay)

    log(f"\n{len(pending)} unique candidates. Checking each one...\n")

    # 2) Inspect ------------------------------------------------------------
    results = []
    stopped = False
    for i, (key, p) in enumerate(pending.items(), 1):
        cached = cache.get(key)
        fresh = cached and (now.timestamp() - cached.get("_checked", 0)) < recheck_after
        if fresh:
            r = dict(cached)
        elif stopped:
            continue
        else:
            try:
                if p["entity"] is not None:
                    r = await inspect_channel(client, p["entity"], args, log)
                elif p["kind"] == "invite":
                    r = await inspect_invite(client, p["value"], args, log)
                else:
                    r = await inspect_username(client, p["value"], args, log)
            except FloodTooLong as e:
                log(f"⛔ Telegram rate limit: must wait {e.seconds // 60} min. "
                    "Saving what we have — run again later to continue.")
                stopped = True
                continue
            r["_checked"] = now.timestamp()
            r["first_seen"] = (cached or {}).get("first_seen") or now.strftime("%Y-%m-%d")
            cache[key] = r
            if i % 10 == 0:
                save_json(cache_path, cache)
            await asyncio.sleep(args.delay)

        prev_kw = set((cached or {}).get("keywords", "").split(" | ")) - {""}
        r["keywords"] = " | ".join(sorted(prev_kw | p["keywords"]))
        r["found_via"] = " + ".join(sorted(p["via"]))
        cache[key]["keywords"] = r["keywords"]
        r["_new"] = not cached
        results.append(r)
        mark = "✅" if passes(r, args, p["keywords"]) else "  "
        log(f"{mark} [{i}/{len(pending)}] {r.get('title') or r.get('link')} — {r.get('status')}")

    save_json(cache_path, cache)
    await client.disconnect()

    # 3) Write outputs ------------------------------------------------------
    good = [r for r in results if passes(r, args, set(r["keywords"].split(" | ")))]
    good.sort(key=lambda r: -(r.get("members") or 0))
    stamp = now.strftime("%Y%m%d-%H%M")
    good_path = out_dir / f"groups_{stamp}.csv"
    all_path = out_dir / f"all_checked_{stamp}.csv"
    write_csv(good_path, good)
    write_csv(all_path, results)

    # running master list of every good group ever found
    master_path = out_dir / "groups_master.csv"
    master = {}
    if master_path.exists():
        with open(master_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                master[row["link"]] = row
    for r in good:
        master[r["link"]] = {**master.get(r["link"], {}), **{k: r.get(k, "") for k in COLUMNS}}
    write_csv(master_path, sorted(master.values(), key=lambda r: -int(r.get("members") or 0)))

    new_good = [r for r in good if r.get("_new")]
    log("\n" + "=" * 60)
    log(f"Groups that allow text and charge no Stars: {len(good)}  (new this run: {len(new_good)})")
    for r in good[:25]:
        flag = " 🆕" if r.get("_new") else ""
        extra = " · needs approval" if r.get("join_approval") else ""
        extra += f" · slow mode {r['slowmode_s']}s" if r.get("slowmode_s") else ""
        log(f"  {r.get('members') or '?':>7} members  {r.get('title')}  {r['link']}{extra}{flag}")
    if len(good) > 25:
        log(f"  ... and {len(good) - 25} more in the CSV")
    log(f"\nThis run:     {good_path}")
    log(f"Everything:   {all_path}")
    log(f"Master list:  {master_path}")
    if stopped:
        log("\nNote: stopped early because of Telegram's rate limit. Re-run later; "
            "already-checked groups are cached and won't be re-checked.")


def main():
    ap = argparse.ArgumentParser(
        description="Find Telegram groups by keyword that allow text and don't charge Stars.")
    ap.add_argument("-k", "--keywords", nargs="+", help="keywords to search")
    ap.add_argument("-f", "--file", help="text file with one keyword per line")
    ap.add_argument("--bot", default="ai_crawlerbot", help="search bot username (default: ai_crawlerbot)")
    ap.add_argument("--template", default="{kw}",
                    help='what to send the bot; {kw} is replaced by the keyword (e.g. "/search {kw}")')
    ap.add_argument("--pages", type=int, default=5, help="max result pages to click through per keyword")
    ap.add_argument("--bot-wait", type=float, default=25, help="max seconds to wait for the bot's reply")
    ap.add_argument("--bot-idle", type=float, default=4, help="seconds of silence = bot finished replying")
    ap.add_argument("--no-bot", action="store_true", help="skip the bot, use global search only")
    ap.add_argument("--global-search", "-g", action="store_true",
                    help="also use Telegram's built-in public search")
    ap.add_argument("--global-limit", type=int, default=50)
    ap.add_argument("--min-members", type=int, default=0)
    ap.add_argument("--max-slowmode", type=int, default=None,
                    help="drop groups whose slow mode is longer than this many seconds")
    ap.add_argument("--no-approval", action="store_true",
                    help="drop groups where admins must approve join requests")
    ap.add_argument("--match-keyword", action="store_true",
                    help="keep only groups whose title/username/description contains the keyword")
    ap.add_argument("--include-unverified", action="store_true",
                    help="also list private invite-link groups whose rules can't be checked without joining")
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between Telegram lookups")
    ap.add_argument("--max-flood-wait", type=int, default=120,
                    help="wait out rate limits up to this many seconds; longer ones stop the run")
    ap.add_argument("--recheck-days", type=float, default=7, help="re-check cached groups after N days")
    ap.add_argument("--out", default="results", help="output folder")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--session", default="finder_session", help="login session file name")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
