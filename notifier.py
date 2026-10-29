#!/usr/bin/env python3
"""
Internship notifier: polls SimplifyJobs listing repos and posts newly added
roles to Discord via webhook. Settings live in config.toml.

Standard library only (Python 3.11+ for tomllib) - no pip install needed.

Secrets (environment variables, set as GitHub repository secrets):
  DISCORD_WEBHOOK_URL            required; default channel for every feed
  DISCORD_WEBHOOK_URL_OFFSEASON  optional; separate channel for off-season
  DISCORD_WEBHOOK_URL_NEWGRAD    optional; separate channel for new grad

CLI flags:
  --dry-run            print what would be sent; don't post or save state
  --file URL=PATH      use a local file for a source URL (repeatable; testing)
  --test               send a test message to each feed's channel and exit
"""
import argparse
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.toml")
STATE_FILE = os.path.join(HERE, "state.json")
USER_AGENT = "internship-notifier/2.0"
DEFAULT_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"

FILTER_DEFAULTS = {
    "us_only": True,
    "hide_no_sponsorship": False,
    "categories": [],
    "terms": [],
    "include_no_term": False,
    "title_keywords": [],
    "exclude_keywords": [],
    "location_keywords": [],
    "max_age_days": 14,
    "max_per_run": 40,
    "only_in_readme": True,
}

CATEGORY_COLORS = {  # Discord embed side-bar colours
    "software": 0x5865F2,
    "ai": 0x57F287,
    "data": 0x57F287,
    "quant": 0xFEE75C,
    "product": 0xEB459E,
    "hardware": 0xED4245,
}


# ---------------------------------------------------------------- config ---

def load_config():
    with open(CONFIG_FILE, "rb") as f:
        raw = tomllib.load(f)
    shared = {k: raw.get(k, v) for k, v in FILTER_DEFAULTS.items()}
    feeds = []
    for fd in raw.get("feeds", []):
        feed = {**shared, **fd}
        for key in ("terms", "categories", "title_keywords",
                    "exclude_keywords", "location_keywords"):
            feed[key] = [str(s).strip().lower() for s in feed.get(key) or [] if str(s).strip()]
        feed.setdefault("emoji", "🆕")
        feed.setdefault("link", "")
        feed["webhook_url"] = (os.environ.get(feed.get("webhook") or DEFAULT_WEBHOOK_ENV, "").strip()
                               or os.environ.get(DEFAULT_WEBHOOK_ENV, "").strip())
        feeds.append(feed)
    if not feeds:
        sys.exit("config.toml has no [[feeds]].")
    return feeds


# ----------------------------------------------------------------- state ---
# {"sources": {url: {"etag": str|None, "seen": [ids]}}, "seeded": [feed names]}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        if "sources" in state:
            return state
    return {"sources": {}, "seeded": []}


def save_state(state):
    for src in state["sources"].values():
        src["seen"] = sorted(set(src["seen"]))
    state["seeded"] = sorted(set(state["seeded"]))
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=0)
        f.write("\n")


# ----------------------------------------------------------------- fetch ---

def fetch_listings(url, etag=None):
    """Returns (listings or None if unchanged, etag)."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


_readme_cache = {}


def fetch_readme(url):
    """Raw README text (HTML entities decoded), cached for this run."""
    if url not in _readme_cache:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=90) as resp:
            _readme_cache[url] = html.unescape(resp.read().decode("utf-8", "replace"))
    return _readme_cache[url]


def in_readme(item, readme_text):
    # Each README row links to simplify.jobs/p/<id>; fall back to the apply URL.
    return item["id"] in readme_text or (item.get("url") and item["url"] in readme_text)


# ------------------------------------------------------------- locations ---

US_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
US_STATE_NAMES = {s.lower() for s in (
    "Alabama,Alaska,Arizona,Arkansas,California,Colorado,Connecticut,Delaware,Florida,"
    "Georgia,Hawaii,Idaho,Illinois,Indiana,Iowa,Kansas,Kentucky,Louisiana,Maine,Maryland,"
    "Massachusetts,Michigan,Minnesota,Mississippi,Missouri,Montana,Nebraska,Nevada,"
    "New Hampshire,New Jersey,New Mexico,New York,North Carolina,North Dakota,Ohio,Oklahoma,"
    "Oregon,Pennsylvania,Rhode Island,South Carolina,South Dakota,Tennessee,Texas,Utah,"
    "Vermont,Virginia,Washington,West Virginia,Wisconsin,Wyoming,District of Columbia,"
    "Puerto Rico").split(",")}
US_ALIASES = {"nyc", "sf", "la", "south sf", "dc", "us", "u.s", "usa", "united states"}
NO_COUNTRY_WORDS = ("remote", "hybrid", "multiple", "anywhere", "various", "nationwide")


def is_us_location(loc):
    """True for US locations and for ones that name no country at all (e.g.
    "Remote"). False for anything clearly abroad (", UK", ", Canada",
    "Remote in Germany", "Singapore", ...)."""
    s = loc.strip().rstrip(".")
    low = s.lower()
    if low in US_ALIASES or low in US_STATE_NAMES or s in US_STATE_CODES:
        return True
    if re.search(r"\b(usa|united states|u\.s\.)", low):
        return True
    m = re.search(r"[,.]\s*([A-Z]{2})$", s)  # "City, ST" (also typo'd "City. ST")
    if m and m.group(1) in US_STATE_CODES:
        return True
    parts = [p.strip() for p in s.split(",")]
    if len(parts) >= 2:  # "City, ON, Canada" / "London, UK" / "Manhattan, NYC"
        last = parts[-1].lower()
        return last in US_STATE_NAMES or last in US_ALIASES
    if low.startswith("remote in "):
        return False
    return any(w in low for w in NO_COUNTRY_WORDS)


# ---------------------------------------------------------------- filter ---

def contains_any(text, keywords):
    """Substring match; short keywords (<=3 chars, e.g. "ny", "ca", "ai")
    must match a whole word so "ny" doesn't hit "Sunnyvale"."""
    text = (text or "").lower()
    for k in keywords:
        if len(k) <= 3:
            if re.search(rf"\b{re.escape(k)}\b", text):
                return True
        elif k in text:
            return True
    return False


def is_eligible(item, f, now):
    if not item.get("active") or not item.get("is_visible"):
        return False
    terms = [t for t in item.get("terms") or [] if t and t.upper() != "N/A"]
    if f["terms"]:
        if terms:
            if not any(contains_any(t, f["terms"]) for t in terms):
                return False
        elif not f["include_no_term"]:
            return False
    if f["categories"] and not contains_any(item.get("category"), f["categories"]):
        return False
    title = item.get("title", "")
    if f["title_keywords"] and not contains_any(title, f["title_keywords"]):
        return False
    if f["exclude_keywords"] and contains_any(title, f["exclude_keywords"]):
        return False
    locs = item.get("locations") or []
    if f["us_only"] and locs and not any(is_us_location(l) for l in locs):
        return False
    if f["location_keywords"] and not any(contains_any(l, f["location_keywords"]) for l in locs):
        return False
    if f["hide_no_sponsorship"] and item.get("sponsorship") in (
        "Does Not Offer Sponsorship", "U.S. Citizenship is Required"
    ):
        return False
    posted = item.get("date_posted") or 0
    if f["max_age_days"] > 0 and now - posted > f["max_age_days"] * 86400:
        return False
    return True


# --------------------------------------------------------------- discord ---

def build_embed(item, feed):
    category = item.get("category") or ""
    color = next((c for k, c in CATEGORY_COLORS.items() if k in category.lower()), 0x99AAB5)
    locs = item.get("locations") or []
    if feed["us_only"]:  # show US locations first
        locs = sorted(locs, key=lambda l: not is_us_location(l))
    loc_text = ", ".join(locs[:4]) + (f" +{len(locs) - 4} more" if len(locs) > 4 else "")
    fields = [
        {"name": "Location", "value": (loc_text or "—")[:1024], "inline": True},
        {"name": "Category", "value": category or "—", "inline": True},
    ]
    sponsorship = item.get("sponsorship")
    if sponsorship and sponsorship != "Other":
        fields.append({"name": "Sponsorship", "value": sponsorship, "inline": True})
    terms = [t for t in item.get("terms") or [] if t and t.upper() != "N/A"]
    embed = {
        "title": f"{item.get('company_name', '?')} — {item.get('title', '?')}"[:256],
        "url": item.get("url") or feed["link"] or None,
        "color": color,
        "fields": fields,
        "footer": {"text": ", ".join(terms) or feed["name"]},
    }
    if item.get("date_posted"):
        embed["timestamp"] = datetime.fromtimestamp(item["date_posted"], timezone.utc).isoformat()
    return embed


def post_discord(webhook, payload, retries=3):
    data = json.dumps(payload).encode()
    for _ in range(retries):
        req = urllib.request.Request(
            webhook, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited - wait as told, then retry
                wait = 2.0
                try:
                    wait = float(json.load(e).get("retry_after", 2))
                except Exception:
                    pass
                time.sleep(wait + 0.5)
                continue
            print(f"Discord error {e.code}: {e.read()[:300]!r}", file=sys.stderr)
            return False
    return False


def send_text(webhook, text, dry_run):
    if dry_run:
        print("  " + text)
        return True
    return post_discord(webhook, {"content": text, "allowed_mentions": {"parse": []}})


def send_listings(feed, items, dry_run):
    """Post cards in batches of 10 (Discord's per-message limit).
    Returns ids that were delivered."""
    delivered = []
    n = len(items)
    for i in range(0, n, 10):
        batch = items[i:i + 10]
        payload = {"embeds": [build_embed(x, feed) for x in batch],
                   "allowed_mentions": {"parse": []}}
        if i == 0:
            payload["content"] = f"{feed['emoji']} **{feed['name']}** · {n} new"
        if dry_run:
            if i == 0:
                print("  " + payload["content"])
            for e in payload["embeds"]:
                print(f"    • {e['title']}  [{e['fields'][0]['value']}]")
            delivered += [x["id"] for x in batch]
            continue
        if not post_discord(feed["webhook_url"], payload):
            break  # stop; undelivered ones are retried next run
        delivered += [x["id"] for x in batch]
        time.sleep(1)
    return delivered


# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file", action="append", default=[], metavar="URL=PATH")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    local_files = dict(s.split("=", 1) for s in args.file)

    feeds = load_config()
    if not args.dry_run:
        missing = [f["name"] for f in feeds if not f["webhook_url"]]
        if missing:
            sys.exit("DISCORD_WEBHOOK_URL is not set (add it as a repository secret).")

    if args.test:
        ok = True
        for webhook in dict.fromkeys(f["webhook_url"] for f in feeds):
            names = ", ".join(f["name"] for f in feeds if f["webhook_url"] == webhook)
            ok &= send_text(webhook, f"✅ Test message — this channel will get: {names}", args.dry_run)
        sys.exit(0 if ok else 1)

    state = load_state()
    now = time.time()
    seeded = set(state["seeded"])

    # Group feeds by source so each file is downloaded once per run.
    by_url = {}
    for f in feeds:
        by_url.setdefault(f["url"], []).append(f)

    for url, url_feeds in by_url.items():
        src = state["sources"].setdefault(url, {"etag": None, "seen": []})
        needs_seed = any(f["name"] not in seeded for f in url_feeds)
        if url in local_files:
            with open(local_files[url]) as fh:
                listings, etag = json.load(fh), None
        else:
            try:
                listings, etag = fetch_listings(url, None if needs_seed else src["etag"])
            except Exception as e:  # one bad source shouldn't stop the others
                print(f"Could not fetch {url}: {e}", file=sys.stderr)
                continue
        if listings is None:
            print(f"Unchanged: {url}")
            continue

        seen = set(src["seen"])
        for feed in url_feeds:
            readme_text = None
            if feed["only_in_readme"] and feed.get("readme"):
                try:
                    readme_text = fetch_readme(feed["readme"])
                except Exception as e:
                    print(f"[{feed['name']}] could not fetch README, skipping: {e}", file=sys.stderr)
                    continue
            eligible = [x for x in listings if x.get("id") and is_eligible(x, feed, now)
                        and (readme_text is None or in_readme(x, readme_text))]
            new = sorted((x for x in eligible if x["id"] not in seen),
                         key=lambda x: x.get("date_posted") or 0)
            print(f"[{feed['name']}] {len(eligible)} match filters, {len(new)} new")

            if feed["name"] not in seeded:
                # First run for this feed: record what exists without spamming.
                seen.update(x["id"] for x in eligible)
                seeded.add(feed["name"])
                send_text(feed["webhook_url"],
                          f"👋 Now watching **{feed['name']}** "
                          f"({len(eligible)} current matches). New ones will show up here.",
                          args.dry_run)
                continue
            if not new:
                continue
            cap = max(1, int(feed["max_per_run"]))
            to_send, overflow = new[-cap:], new[:-cap]
            seen.update(send_listings(feed, to_send, args.dry_run))
            if overflow:
                seen.update(x["id"] for x in overflow)
                send_text(feed["webhook_url"],
                          f"…and {len(overflow)} more new {feed['name']}. Full list: <{feed['link']}>",
                          args.dry_run)

        # A role matching two feeds (e.g. Summer + Winter) is only sent once,
        # because feeds sharing a source share this seen-set.
        live = {x.get("id") for x in listings}
        src["seen"] = [i for i in seen if i in live]
        src["etag"] = etag

    state["seeded"] = list(seeded)
    if args.dry_run:
        print("(dry run - state not saved)")
    else:
        save_state(state)


if __name__ == "__main__":
    main()
