# Group Finder Bot

Your own private Telegram bot. You send it keywords from your phone, and it:

1. searches for groups (through **@ai_crawlerbot**, and optionally Telegram's own search)
2. checks every group it finds and keeps only the ones that **allow text messages** and **don't charge Telegram Stars**
3. sends you the list, each group with a **➕ Join** button (plus **Join all**)
4. joins them from your account, one at a time and slowly, so Telegram doesn't flag you
5. can repeat your saved keywords **every day** and tell you only about **new** groups, and **auto-join** them if you switch that on

No terminal needed. It runs on Railway; you only ever use the Telegram chat.

---

## Setup (about 20 minutes, one time)

You need four things: a **bot token**, your **API keys**, a **GitHub** copy of these files, and a **Railway** service.

### Step 1 — Create your bot (2 min)
1. In Telegram, open **@BotFather** and send `/newbot`.
2. Pick a name and a username ending in `bot` (e.g. `abebe_groupfinder_bot`).
3. BotFather replies with a **token** like `7012345678:AAH...`. Save it. This is your **BOT_TOKEN**.

### Step 2 — Get your API keys (3 min)
1. Go to **https://my.telegram.org** and log in with your phone number (the code arrives in your Telegram app).
2. Click **API development tools**.
3. Fill in any app title (e.g. "Group Finder"), any short name, platform **Desktop**, and submit.
4. Save the **App api_id** (a number) and **App api_hash** (a long string).

### Step 3 — Put the files on GitHub (3 min)
1. Sign in at **https://github.com** and click **+ → New repository**.
2. Name it (e.g. `group-finder`), choose **Private**, and click **Create repository**.
3. On the new page, click **uploading an existing file**.
4. Unzip the download, then drag these four files into the browser: `main.py`, `tg_group_finder.py`, `requirements.txt`, `README.md`.
5. Click **Commit changes**.

### Step 4 — Deploy on Railway (5 min)
1. Go to **https://railway.com**, sign in **with GitHub**.
2. Click **New Project → Deploy from GitHub repo** and pick your `group-finder` repo. (If it isn't listed, click *Configure GitHub App* and give Railway access to it.)
3. Open the service it created, go to the **Variables** tab and add:

   | Variable | Value |
   |---|---|
   | `API_ID` | your api_id from step 2 |
   | `API_HASH` | your api_hash from step 2 |
   | `BOT_TOKEN` | the token from step 1 |
   | `ACCESS_CODE` | any secret word you make up, e.g. `blue-mango-77` |

4. **Add a volume** (this is where your login and found groups are saved; without it everything resets on every restart): right-click the service on the project canvas, or use the command palette (Ctrl/⌘ + K), choose **Add Volume**, and set the mount path to **`/data`**.
5. Railway redeploys automatically. Open **Deployments → View logs**. You should see `Bot running as @your_bot`.

### Step 5 — Connect it to your account (2 min)
1. In Telegram, open **your** bot and send: `/start blue-mango-77` (your ACCESS_CODE). The bot is now yours and ignores everyone else.
2. Send `/login`, then your phone number with country code.
3. Telegram sends you a login code. Type it back to the bot **with spaces between the digits**, e.g. `1 2 3 4 5`. Telegram blocks the login if the code is sent as one number.
4. If you use two-step verification, the bot asks for your password and deletes your message right after.

Done. ✅

---

## Using it

| What you want | What to send |
|---|---|
| Search now | just type keywords: `crypto chat, forex traders` |
| Join a group | tap **➕ Join** under it (or **Join all**) |
| Save keywords for the daily search | `/watch crypto chat, forex traders` |
| See / remove saved keywords | `/list`, `/unwatch forex traders` |
| Search all saved keywords now | `/run` |
| Turn on auto-join, change filters and limits | `/settings` |
| See what's waiting to be joined | `/queue` |
| Download every group found (Excel/Sheets) | `/groups` |
| Stop a running search | `/cancel` |

### Settings (tap to change)
- **Auto-join**: joins every new group the search finds (off by default)
- **Joins/day**: daily cap, 20 by default
- **Mute after join**: silences notifications from groups it joins (on by default, since dozens of groups get noisy fast)
- **Telegram search**: also uses Telegram's built-in search for more results
- **Skip approval**: ignores groups where an admin must approve you
- **Keyword in name**: keeps only groups whose name or description contains your keyword
- **Min members**: skips small groups
- **Auto search**: how often your saved keywords are searched (6, 12, 24, 48 h, or off). It only messages you when there's something new.

---

## Important: staying safe with Telegram

- **Joining is deliberately slow**, about one group every 1–2 minutes, capped per day. Telegram restricts accounts that join many groups quickly. If Telegram tells it to wait, the bot pauses and continues by itself. Raising the daily cap above ~30 increases the risk.
- **Telegram limits how many groups and channels one account can be in** (500, or 1,000 with Premium). The bot tells you if you hit it.
- **Don't use the same account to post identical messages across many groups.** That is the fastest way to get an account limited or banned, and it isn't something this bot does.
- **Your login lives on Railway.** Anyone with access to your Railway project could use your Telegram account, so keep it private and don't share your ACCESS_CODE. To revoke it any time, send `/logout`, or in Telegram go to *Settings → Devices* and end the session.
- Telegram will show a "new login" notice from a server location when you `/login`. That's this bot.

## If something doesn't work
- **Bot doesn't answer:** check Railway logs. "Missing variables" means a variable is misspelled.
- **Everything resets after a restart:** the volume isn't attached or isn't at `/data`.
- **Searches return nothing from @ai_crawlerbot:** the bot may need a command before the keyword. Add a Railway variable `BOT_TEMPLATE` with e.g. `/search {kw}`. Or use another search bot with the variable `SEARCH_BOT`.
- **Login code "expired" right away:** you sent it without spaces. `/login` again.
