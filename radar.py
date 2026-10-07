#!/usr/bin/env python3
"""Lead Radar - watches free public sources for people asking for web/dev work.

Sources (all free, no login, no API keys):
  - Reddit: keyword search across all of Reddit + new-post feeds of job and
    business subreddits
  - Hacker News: new stories/comments via the public Algolia API
  - Discourse forums: Webflow, Bubble, Framer (latest topics, keyword filter)
  - Optional: Google News / Bing News RSS, any extra RSS feed you add

Output:
  - leads.csv  (every lead ever found, opens in Excel)
  - console    (live feed)
  - Telegram   (instant phone alerts, optional - add bot token in config.json)

Usage:
  python radar.py          run forever, poll every N minutes (default 10)
  python radar.py --once   one sweep then exit (good for testing)
Stop with Ctrl+C or by closing the window.

Standard library only - nothing to install.
"""

from __future__ import annotations

import csv
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOCAL_CONFIG_PATH = BASE_DIR / "config.local.json"  # gitignored, holds secrets
SEEN_PATH = BASE_DIR / "seen.json"
STATE_PATH = BASE_DIR / "state.json"
LEADS_CSV_PATH = BASE_DIR / "leads.csv"

# Reddit (and some CDNs) hard-block any UA that looks like a script - a
# plain browser UA is required for the public RSS endpoints.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_GAP_SECONDS = 1.0
# Reddit allows unauthenticated reads very sparingly per IP and throttles
# bursts, so its requests are rotated (different slice each cycle) and paced
# far apart. Everything else runs in parallel while Reddit takes its time.
REDDIT_GAP_SECONDS = 40
SEARCH_WINDOW = 24      # keywords searched site-wide per cycle (rotates)
SEARCH_BATCH = 8        # phrases OR-ed per Reddit search request
ROTATING_SUBS_PER_CYCLE = 3
HTTP_TIMEOUT = 25

_HOST_BLOCKED: dict[str, str] = {}


class HostBlocked(Exception):
    """Site refused us (429/403) twice - skip it for the rest of this cycle."""

CSV_HEADER = ["found_at_utc", "source", "title", "url", "matched", "snippet"]

DEFAULT_CONFIG = {
    "poll_interval_minutes": 10,
    "max_alerts_per_cycle": 15,
    "hours_to_look_back": 24,
    "keywords": [
        "looking for a web developer",
        "looking for a developer",
        "need a web developer",
        "need a website",
        "need a website built",
        "need a website for my",
        "need a website created",
        "want a website built",
        "build me a website",
        "build my website",
        "recommend a web developer",
        "web developer needed",
        "website developer needed",
        "developer needed for",
        "looking for someone to build",
        "looking for someone to make me",
        "need someone to build",
        "fix my website",
        "website redesign",
        "redesign my website",
        "need an app built",
        "looking for an app developer",
        "app developer needed",
        "shopify expert needed",
        "need a shopify developer",
        "shopify developer needed",
        "wordpress developer needed",
        "need a wordpress developer",
        "woocommerce developer",
        "need a python script",
        "looking for a python developer",
        "automation developer needed",
        "hiring freelance web developer",
        "freelance web developer needed",
        "looking for freelance developer",
        "landing page needed",
        "need a landing page",
        "busco desarrollador web",
        "necesito una pagina web",
        "necesito un programador",
        "procuro programador",
        "preciso de um site",
        "cherche developpeur",
        "besoin d'un site web",
        "suche webentwickler",
        "brauche eine webseite",
        "нужен программист",
        "ищу веб-разработчика",
        "أحتاج مبرمج",
        "مطلوب مبرمج"
    ],
    "block_keywords": [
        "[for hire]", "(for hire)", "[portfolio]", "hire me", "available for",
        "internship", "pirat", "free movies", "stream movies",
        "download music", "torrent", "looking for a job", "job hunting"
    ],
    "hiring_tag_keywords": ["[hiring]", "(hiring)"],
    "job_subreddits": [
        "forhire", "NeedAWebsite", "jobbit", "webdevsforhire",
        "DesignJobs", "b2bforhire", "gameDevClassifieds"
    ],
    "community_subreddits": [
        "smallbusiness", "ecommerce", "shopify", "Entrepreneur",
        "dropshipping", "nocode", "automation", "saas"
    ],
    "city_subreddits": [
        "london", "nyc", "LosAngeles", "chicago", "toronto", "sydney",
        "melbourne", "dubai", "singapore", "bangalore", "mumbai", "delhi",
        "berlin", "amsterdam", "austin"
    ],
    "hn_enabled": True,
    "discourse_forums": [
        {"name": "Bubble Forum", "url": "https://forum.bubble.io"},
        {"name": "Adalo Forum", "url": "https://forum.adalo.com"},
        {"name": "Softr Community", "url": "https://community.softr.io"}
    ],
    "google_news": {
        "enabled": False,
        "phrases": [
            "looking for a web developer",
            "need a website built",
            "web developer needed",
            "hiring freelance web developer",
            "busco desarrollador web"
        ]
    },
    "bing_news": {
        "enabled": False,
        "phrases": [
            "looking for a web developer",
            "need a website built",
            "web developer needed"
        ]
    },
    "extra_rss_feeds": [],
    "telegram": {"bot_token": "", "chat_id": ""}
}


# ---------------------------------------------------------------- utilities

def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def norm_text(value: str) -> str:
    """lowercase, strip accents, collapse whitespace - so matching is loose."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value.lower()).strip()


def strip_html(raw: str) -> str:
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def http_get(url: str) -> bytes:
    host = urllib.parse.urlsplit(url).netloc
    if host in _HOST_BLOCKED:
        raise HostBlocked(host)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (502, 504) and attempt == 1:
                time.sleep(15)
                continue
            if exc.code in (403, 429, 503):
                if attempt == 1:
                    wait = 30
                    try:
                        wait = min(int(exc.headers.get("Retry-After", 30)), 45)
                    except (TypeError, ValueError):
                        pass
                    log(f"  {host} throttled us ({exc.code}) - waiting {wait}s, retrying once")
                    time.sleep(wait)
                    continue
                _HOST_BLOCKED[host] = exc.code
                log(f"  {host} still throttled ({exc.code}) - skipping it for this cycle")
                raise HostBlocked(host)
            raise
    raise RuntimeError("unreachable")


def make_lead(source, title, url, snippet, matched, guid) -> dict | None:
    if not title and not url:
        return None
    key = f"{source}|{guid or url or title}"
    lead_id = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    return {
        "id": lead_id,
        "source": source,
        "title": strip_html(title) or "(no title)",
        "url": (url or "").strip(),
        "snippet": (snippet or "")[:400],
        "matched": matched,
    }


def match_keywords(text: str, keywords: list[str], block: list[str]) -> list[str]:
    hay = norm_text(text)
    if not hay:
        return []
    for b in block:
        if b and b in hay:
            return []
    return [kw for kw in keywords if kw in hay]


# ------------------------------------------------------------ feed parsing

def parse_feed(data: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom into [{title,url,raw,guid}] - works for both."""
    root = ET.fromstring(data)
    entries: list[dict] = []
    tag = root.tag.rsplit("}", 1)[-1]
    if tag == "feed":  # Atom (Reddit)
        ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        for entry in root.findall(f"{{{ns}}}entry"):
            link = ""
            for lnk in entry.findall(f"{{{ns}}}link"):
                href = lnk.get("href") or (lnk.text or "").strip()
                if href:
                    link = href
                    if lnk.get("rel") in (None, "alternate"):
                        break
            raw = entry.findtext(f"{{{ns}}}content") or entry.findtext(f"{{{ns}}}summary") or ""
            entries.append({
                "title": (entry.findtext(f"{{{ns}}}title") or "").strip(),
                "url": link,
                "raw": raw,
                "guid": entry.findtext(f"{{{ns}}}id") or link,
            })
    else:  # RSS 2.0 (news engines, extra feeds)
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            entries.append({
                "title": (item.findtext("title") or "").strip(),
                "url": link,
                "raw": item.findtext("description") or "",
                "guid": (item.findtext("guid") or link).strip(),
            })
    return entries


# ------------------------------------------------------------------ sources

def fetch_reddit_search(cfg: dict) -> list[dict]:
    tparam = "day" if cfg["hours_to_look_back"] <= 24 else "week"
    phrases = [kw.strip() for kw in cfg["keywords"] if kw.strip()]
    if not phrases:
        return []
    # Reddit's OR search silently returns nothing for big query blobs, and it
    # throttles request volume - so each cycle searches a rotating window of
    # keywords in small OR batches. Over a few cycles every keyword is covered.
    state = load_state()
    offset = state.get("search_offset", 0) % len(phrases)
    window = [phrases[(offset + i) % len(phrases)] for i in range(min(SEARCH_WINDOW, len(phrases)))]
    state["search_offset"] = (offset + SEARCH_WINDOW) % len(phrases)
    save_state(state)
    leads = []
    chunks = [window[i:i + SEARCH_BATCH] for i in range(0, len(window), SEARCH_BATCH)]
    for chunk in chunks:
        q = urllib.parse.quote(" OR ".join(f'("{p}")' for p in chunk))
        url = f"https://www.reddit.com/search.rss?q={q}&sort=new&t={tparam}&limit=100"
        try:
            entries = parse_feed(http_get(url))
        except HostBlocked:
            break
        except Exception as exc:
            log(f"  reddit search batch: {exc}")
            continue
        for e in entries:
            snippet = strip_html(e["raw"])
            hits = match_keywords(e["title"] + " " + snippet,
                                  cfg["_keywords"], cfg["_block"])
            if not hits:
                continue
            lead = make_lead("Reddit search", e["title"], e["url"],
                             snippet, ", ".join(hits), e["guid"])
            if lead:
                leads.append(lead)
        time.sleep(REDDIT_GAP_SECONDS)
    return leads


def fetch_reddit_subreddits(cfg: dict) -> list[dict]:
    # Job subreddits are checked every cycle; community/city subreddits rotate
    # so the whole pool still gets covered while staying under Reddit's rate
    # limits.
    pool = cfg["community_subreddits"] + cfg["city_subreddits"]
    state = load_state()
    offset = state.get("reddit_offset", 0) % max(1, len(pool))
    rotating = pool[offset:offset + ROTATING_SUBS_PER_CYCLE]
    if len(rotating) < ROTATING_SUBS_PER_CYCLE:
        rotating += pool[:ROTATING_SUBS_PER_CYCLE - len(rotating)]
    state["reddit_offset"] = (offset + ROTATING_SUBS_PER_CYCLE) % max(1, len(pool))
    save_state(state)
    if rotating:
        log(f"  subreddit rotation this cycle: {', '.join(rotating)}")
    leads = []
    for sub in list(cfg["job_subreddits"]) + rotating:
        url = f"https://www.reddit.com/r/{sub}/new/.rss?limit=100"
        try:
            entries = parse_feed(http_get(url))
        except HostBlocked:
            break
        except Exception as exc:
            log(f"  r/{sub}: {exc}")
            continue
        for e in entries:
            snippet = strip_html(e["raw"])
            title_low = norm_text(e["title"])
            hits = match_keywords(e["title"] + " " + snippet,
                                  cfg["_keywords"], cfg["_block"])
            tags = [t for t in cfg["_hiring_tags"] if t in title_low]
            if not hits and not tags:
                continue
            matched = ", ".join(hits + tags)
            lead = make_lead(f"Reddit r/{sub}", e["title"], e["url"],
                             snippet, matched, e["guid"])
            if lead:
                leads.append(lead)
        time.sleep(REDDIT_GAP_SECONDS)
    return leads


def fetch_hackernews(cfg: dict) -> list[dict]:
    if not cfg.get("hn_enabled"):
        return []
    since = int(time.time()) - cfg["hours_to_look_back"] * 3600
    leads = []
    for kw in cfg["keywords"]:
        if not kw.strip():
            continue
        url = (
            "https://hn.algolia.com/api/v1/search_by_date"
            "?tags=%28story%2Ccomment%29"
            f"&query={urllib.parse.quote(kw.strip())}"
            f"&numericFilters=created_at_i%3E{since}&hitsPerPage=20"
        )
        try:
            hits_json = json.loads(http_get(url).decode("utf-8", "replace"))
        except HostBlocked:
            break
        except Exception as exc:
            log(f"  HN '{kw[:40]}': {exc}")
            continue
        for hit in hits_json.get("hits", []):
            title = hit.get("title") or hit.get("story_title") or "HN comment"
            body = strip_html(hit.get("story_text") or hit.get("comment_text") or "")
            matched = match_keywords(title + " " + body,
                                     cfg["_keywords"], cfg["_block"])
            if not matched:
                continue
            link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            lead = make_lead("Hacker News", title, link, body,
                             ", ".join(matched), f"hn-{hit.get('objectID')}")
            if lead:
                leads.append(lead)
        time.sleep(REQUEST_GAP_SECONDS)
    return leads


def fetch_discourse_forums(cfg: dict) -> list[dict]:
    leads = []
    lookback = cfg["hours_to_look_back"] * 3600
    now = time.time()
    for forum in cfg.get("discourse_forums", []):
        base = str(forum.get("url", "")).rstrip("/")
        if not base:
            continue
        name = forum.get("name", base)
        try:
            payload = json.loads(
                http_get(f"{base}/latest.json?order=created").decode("utf-8", "replace")
            )
            topics = payload.get("topic_list", {}).get("topics", [])
        except HostBlocked:
            break
        except Exception as exc:
            log(f"  {name}: {exc}")
            continue
        for topic in topics:
            title = str(topic.get("title", ""))
            created = str(topic.get("created_at", ""))
            try:
                age = now - datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                age = 0
            if age > lookback:
                continue
            matched = match_keywords(title, cfg["_keywords"], cfg["_block"])
            if not matched:
                continue
            link = f"{base}/t/{topic.get('slug') or 'topic'}/{topic.get('id')}"
            lead = make_lead(name, title, link, "", ", ".join(matched),
                             f"{base}-{topic.get('id')}")
            if lead:
                leads.append(lead)
        time.sleep(REQUEST_GAP_SECONDS)
    return leads


def fetch_news_engine(cfg: dict, key: str, base_url_fn) -> list[dict]:
    settings = cfg.get(key) or {}
    if not settings.get("enabled"):
        return []
    leads = []
    for phrase in settings.get("phrases", []):
        if not phrase.strip():
            continue
        url = base_url_fn(urllib.parse.quote(f'"{phrase.strip()}"'))
        try:
            entries = parse_feed(http_get(url))
        except Exception as exc:
            log(f"  {key} '{phrase[:40]}': {exc}")
            continue
        for e in entries:
            snippet = strip_html(e["raw"])
            matched = match_keywords(e["title"] + " " + snippet,
                                     cfg["_keywords"], cfg["_block"])
            if not matched:
                continue
            lead = make_lead(("Google News" if key == "google_news" else "Bing News"),
                             e["title"], e["url"], snippet, ", ".join(matched), e["guid"])
            if lead:
                leads.append(lead)
        time.sleep(REQUEST_GAP_SECONDS)
    return leads


def fetch_google_news(cfg: dict) -> list[dict]:
    return fetch_news_engine(
        cfg, "google_news",
        lambda q: f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")


def fetch_bing_news(cfg: dict) -> list[dict]:
    return fetch_news_engine(
        cfg, "bing_news",
        lambda q: f"https://www.bing.com/news/search?q={q}&format=RSS")


def fetch_extra_rss(cfg: dict) -> list[dict]:
    leads = []
    for feed in cfg.get("extra_rss_feeds", []):
        name, url = str(feed.get("name", "RSS")), str(feed.get("url", ""))
        if not url:
            continue
        try:
            entries = parse_feed(http_get(url))
        except HostBlocked:
            break
        except Exception as exc:
            log(f"  {name}: {exc}")
            continue
        for e in entries:
            snippet = strip_html(e["raw"])
            matched = match_keywords(e["title"] + " " + snippet,
                                     cfg["_keywords"], cfg["_block"])
            if not matched:
                continue
            lead = make_lead(name, e["title"], e["url"], snippet,
                             ", ".join(matched), e["guid"])
            if lead:
                leads.append(lead)
        time.sleep(REQUEST_GAP_SECONDS)
    return leads


# ------------------------------------------------------------------- state

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"config.json unreadable ({exc}), using defaults")
            stored = {}
    else:
        stored = {}
    if LOCAL_CONFIG_PATH.exists():
        try:
            stored.update(json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            log(f"config.local.json unreadable ({exc}) - ignored")
    cfg = {**DEFAULT_CONFIG, **stored}
    cfg["_keywords"] = [norm_text(k) for k in cfg["keywords"] if k.strip()]
    cfg["_block"] = [norm_text(b) for b in cfg.get("block_keywords", []) if b.strip()]
    cfg["_hiring_tags"] = [norm_text(t) for t in cfg.get("hiring_tag_keywords", [])]
    return cfg


def save_default_config() -> None:
    CONFIG_PATH.write_text(
        json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False),
        encoding="utf-8")


def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        log(f"  could not save state.json: {exc}")


def save_seen(seen: dict) -> None:
    cutoff = time.time() - 30 * 86400
    pruned = {}
    for lead_id, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp).timestamp() >= cutoff:
                pruned[lead_id] = stamp
        except ValueError:
            continue
    if len(pruned) > 50000:
        keep = sorted(pruned.items(), key=lambda kv: kv[1], reverse=True)[:50000]
        pruned = dict(keep)
    try:
        SEEN_PATH.write_text(
            json.dumps(pruned, indent=0), encoding="utf-8")
    except Exception as exc:
        log(f"  could not save seen.json: {exc}")


def append_csv(leads: list[dict]) -> None:
    new_file = not LEADS_CSV_PATH.exists()
    try:
        with LEADS_CSV_PATH.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(CSV_HEADER)
            for lead in leads:
                writer.writerow([
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    lead["source"], lead["title"], lead["url"],
                    lead["matched"], lead["snippet"],
                ])
    except PermissionError:
        log("  leads.csv is open in Excel - close it so new rows can be saved")


# -------------------------------------------------------------- delivering

def telegram_active(cfg: dict) -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                or (cfg.get("telegram") or {}).get("bot_token"))


def telegram_send(cfg: dict, text: str) -> bool:
    tg = cfg.get("telegram") or {}
    # env vars (used on GitHub Actions) take priority over config files
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or str(tg.get("bot_token") or "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id") or "").strip()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    return True


def lead_message(lead: dict) -> str:
    return (
        f"🎯 <b>New lead — {lead['source']}</b>\n\n"
        f"<b>{html_mod.escape(lead['title'])}</b>\n\n"
        f"🔗 {lead['url']}\n"
        f"🔑 matched: {html_mod.escape(lead['matched'])}\n"
        + (f"\n💬 {html_mod.escape(lead['snippet'][:280])}…" if lead["snippet"] else "")
    )


def print_lead(lead: dict) -> None:
    print(f"\n  >>> {lead['source']}")
    print(f"      {lead['title']}")
    print(f"      {lead['url']}")
    print(f"      matched: {lead['matched']}")
    if lead["snippet"]:
        print(f"      {lead['snippet'][:160]}")


def deliver(cfg: dict, leads: list[dict]) -> None:
    if not leads:
        return
    append_csv(leads)
    cap = max(1, int(cfg.get("max_alerts_per_cycle", 15)))
    alerts = leads[:cap]
    telegram_on = telegram_active(cfg)
    for lead in alerts:
        print_lead(lead)
        if telegram_on:
            try:
                telegram_send(cfg, lead_message(lead))
                time.sleep(0.4)
            except Exception as exc:
                log(f"  telegram send failed: {exc}")
                telegram_on = False
    if len(leads) > len(alerts) and telegram_active(cfg):
        try:
            telegram_send(cfg, f"📋 +{len(leads) - len(alerts)} more leads this "
                               f"cycle — see leads.csv")
        except Exception:
            pass


# -------------------------------------------------------------------- main

def run_reddit_phase(cfg: dict) -> list[dict]:
    leads = []
    for fetcher in (fetch_reddit_search, fetch_reddit_subreddits):
        try:
            found = fetcher(cfg) or []
            log(f"{fetcher.__name__.replace('fetch_', '').replace('_', ' ')}: "
                f"{len(found)} matches")
            leads.extend(found)
        except Exception as exc:
            log(f"{fetcher.__name__}: ERROR {exc}")
    return leads


def run_cycle(cfg: dict) -> int:
    _HOST_BLOCKED.clear()
    seen = load_seen()
    buckets: dict[str, dict] = {}

    # Reddit is pace-limited to one request per ~40s; let it take its time in
    # a worker thread while the fast sources are checked on the main thread.
    with ThreadPoolExecutor(max_workers=2) as pool:
        reddit_future = pool.submit(run_reddit_phase, cfg)
        for fetcher in (fetch_hackernews, fetch_discourse_forums,
                        fetch_google_news, fetch_bing_news, fetch_extra_rss):
            try:
                found = fetcher(cfg) or []
                log(f"{fetcher.__name__.replace('fetch_', '')}: {len(found)} matches")
            except Exception as exc:
                log(f"{fetcher.__name__}: ERROR {exc}")
                continue
            for lead in found:
                buckets.setdefault(lead["id"], lead)
        reddit_leads = reddit_future.result()

    for lead in reddit_leads:
        buckets.setdefault(lead["id"], lead)

    fresh = []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for lead in buckets.values():
        if lead["id"] in seen:
            continue
        seen[lead["id"]] = now_iso
        fresh.append(lead)

    save_seen(seen)
    deliver(cfg, fresh)

    tg_note = "telegram on" if telegram_active(cfg) else "telegram NOT configured"
    log(f"cycle done: {len(fresh)} new lead(s) saved to leads.csv ({tg_note})")
    return len(fresh)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not CONFIG_PATH.exists():
        save_default_config()
        log(f"first run: wrote default config to {CONFIG_PATH}")
    cfg = load_config()
    interval = max(1, int(cfg.get("poll_interval_minutes", 10)))

    log("Lead Radar started")
    log(f"keywords: {len(cfg['_keywords'])} | interval: {interval} min | "
        f"output: {LEADS_CSV_PATH}")

    if "--once" in argv:
        run_cycle(cfg)
        return 0

    while True:
        cycle_start = time.time()
        try:
            run_cycle(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"cycle crashed (continuing): {exc}")
        # keep the full loop period near `interval` even if a cycle ran long
        remaining = interval * 60 - (time.time() - cycle_start)
        nap = max(30, remaining)
        log(f"sleeping {nap // 60} min {nap % 60} s - Ctrl+C to stop")
        try:
            time.sleep(nap)
        except KeyboardInterrupt:
            break
    log("stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        log("stopped by user")
        sys.exit(0)
