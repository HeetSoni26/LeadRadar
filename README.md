# Lead Radar

Watches free public websites 24x7 for people posting things like *"I need a
website"*, *"looking for a web developer"*, *"[Hiring] Shopify expert"* — and
saves every lead to a spreadsheet + can ping your phone instantly on Telegram.

**Cost: zero.** No API keys, no subscriptions, nothing to install.

---

## Quick start

1. Double-click **`start_radar.bat`** — a black window opens and starts working.
2. Leave it running. Every new lead is:
   - printed live in that window, and
   - saved to **`leads.csv`** (open it in Excel any time).
3. Stop it by closing the window. Start it again any time.

First run creates `seen.json` (memory of what you already saw, so you never get
the same lead twice) and may show leads up to 24 hours old — that's the
one-time catch-up, after that you only get new posts.

## Get alerts on your phone (Telegram, free, 2 minutes)

1. In Telegram, search for **@BotFather** → send `/newbot` → give it any name
   (e.g. "My Lead Bot") → it gives you a **token** like
   `123456789:AAHf3xxxxxxxxxxxxxxxxxxxxx`.
2. Search for **@userinfobot** → send anything → it replies with your
   **chat id** (a number like `987654321`).
3. Open `config.json` in Notepad, paste both:
   ```json
   "telegram": {
     "bot_token": "123456789:AAHf3xxx...",
     "chat_id": "987654321"
   }
   ```
4. Send `/start` to your own bot once in Telegram, then restart the radar.
   Every new lead now arrives as a phone notification, usually within minutes.

## What it watches

| Source | What |
|---|---|
| Reddit | Site-wide keyword search + 30 subreddits: r/forhire, **r/NeedAWebsite**, r/jobbit, r/webdevsforhire, r/smallbusiness, r/shopify + city subs (r/london, r/dubai, r/mumbai...) |
| Hacker News | New stories + comments (founder-heavy audience) |
| Bubble / Adalo / Softr forums | Latest topics on no-code "hire help" communities |
| Google News / Bing News | Built in but OFF by default (mostly news articles, not clients). Turn on in `config.json` with `"enabled": true` |
| Any RSS feed | Add under `extra_rss_feeds` |

**Good to know about Reddit:** Reddit heavily limits how fast any tool may
read it, so the radar spreads its Reddit checks ~40 seconds apart and rotates
which subreddits and keywords it checks each cycle — over a few cycles
everything gets covered. If Reddit still throttles, the radar skips it and
catches up next cycle automatically. Other sources are checked in parallel
and finish in under 2 minutes.

## Make it yours — edit `config.json` (Notepad is fine)

- **`keywords`** — what people say when they want work done. Add your own, in
  any language. Accents are ignored when matching (so `cherche developpeur`
  also matches `cherche développeur`).
- **`city_subreddits`** — add more cities, e.g. `"paris"`, `"toronto"`.
- **`poll_interval_minutes`** — how often it checks (10 = good; 15–20 is
  gentler on the sites, still fast).
- **`block_keywords`** — anything containing these is ignored
  (default blocks `[for hire]` posts — those are other freelancers advertising,
  not clients).
- After editing, save and restart the radar.

## What it deliberately does NOT watch

**LinkedIn, Instagram, Facebook, X/Twitter.** There is no free, safe way to
monitor these automatically — any bot using your account breaks their rules
and they **permanently ban** accounts for it. Don't give your passwords to any
tool claiming otherwise. Those platforms you check manually (saved searches on
X, Facebook Group keyword alerts, LinkedIn job filters) — everything else is
covered here automatically.

## Going truly 24x7 (even when your PC is off)

While your PC is on, the radar works. For 24x7 for free, the easiest next step
is running it as a scheduled job on **GitHub Actions** (free for public repos)
— ask and it can be set up — or a free Oracle Cloud always-free server.

## Files

- `radar.py` — the program (don't edit)
- `config.json` — your settings (edit this)
- `leads.csv` — every lead found, ever (opens in Excel)
- `seen.json` — memory so repeats are suppressed
- `start_radar.bat` — double-click to run
