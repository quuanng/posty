#!/usr/bin/env python3
"""
Internship notifier: polls SimplifyJobs/Summer2027-Internships and posts
newly added listings to a Discord channel via webhook.

Standard library only - no pip install needed.

Config comes from environment variables (set them in the workflow file):
  DISCORD_WEBHOOK_URL  required (store as a GitHub secret, never in code)
  TERMS                comma list, e.g. "Summer 2027"            (blank = any)
  CATEGORIES           comma list of keywords, e.g. "software,ai,quant"
                       matched against category, case-insensitive (blank = all)
  TITLE_KEYWORDS       only notify if title contains one of these (blank = any)
  EXCLUDE_KEYWORDS     skip if title contains any of these       (blank = none)
  LOCATION_KEYWORDS    only notify if a location contains one of these
                       (blank = any), e.g. "remote,ny,ca,seattle"
  HIDE_NO_SPONSORSHIP  "true" to skip roles that don't sponsor / need US citizenship
  MAX_AGE_DAYS         ignore listings posted more than N days ago (default 14)
  MAX_PER_RUN          cap on cards per run; extras are summarised (default 40)

CLI flags:
  --dry-run            print what would be sent; don't post or save state
  --file PATH          read listings from a local file instead of GitHub
  --test               send a single test message to Discord and exit
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
    "dev/.github/scripts/listings.json"
)
REPO_URL = "https://github.com/SimplifyJobs/Summer2027-Internships"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
USER_AGENT = "internship-notifier/1.0"

CATEGORY_COLORS = {  # Discord embed side-bar colours
    "software": 0x5865F2,
    "ai": 0x57F287,
    "data": 0x57F287,
    "quant": 0xFEE75C,
    "product": 0xEB459E,
    "hardware": 0xED4245,
}


# ---------------------------------------------------------------- config ---

def env_list(name):
    raw = os.environ.get(name, "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def load_config():
    return {
        "webhook": os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
        "terms": env_list("TERMS"),
        "categories": env_list("CATEGORIES"),
        "title_keywords": env_list("TITLE_KEYWORDS"),
        "exclude_keywords": env_list("EXCLUDE_KEYWORDS"),
        "location_keywords": env_list("LOCATION_KEYWORDS"),
        "hide_no_sponsorship": os.environ.get("HIDE_NO_SPONSORSHIP", "").lower() == "true",
        "max_age_days": float(os.environ.get("MAX_AGE_DAYS") or 14),
        "max_per_run": int(os.environ.get("MAX_PER_RUN") or 40),
    }


# ----------------------------------------------------------------- state ---

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=0)
        f.write("\n")


# ----------------------------------------------------------------- fetch ---

def fetch_listings(etag=None):
    """Returns (listings or None if unchanged, new_etag)."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(LISTINGS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


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


def is_eligible(item, cfg, now):
    if not item.get("active") or not item.get("is_visible"):
        return False
    if cfg["terms"] and not any(contains_any(t, cfg["terms"]) for t in item.get("terms") or []):
        return False
    if cfg["categories"] and not contains_any(item.get("category"), cfg["categories"]):
        return False
    title = item.get("title", "")
    if cfg["title_keywords"] and not contains_any(title, cfg["title_keywords"]):
        return False
    if cfg["exclude_keywords"] and contains_any(title, cfg["exclude_keywords"]):
        return False
    locs = item.get("locations") or []
    if cfg["location_keywords"] and not any(contains_any(l, cfg["location_keywords"]) for l in locs):
        return False
    if cfg["hide_no_sponsorship"] and item.get("sponsorship") in (
        "Does Not Offer Sponsorship", "U.S. Citizenship is Required"
    ):
        return False
    posted = item.get("date_posted") or 0
    if cfg["max_age_days"] > 0 and now - posted > cfg["max_age_days"] * 86400:
        return False
    return True


# --------------------------------------------------------------- discord ---

def build_embed(item):
    category = item.get("category") or ""
    color = next((c for k, c in CATEGORY_COLORS.items() if k in category.lower()), 0x99AAB5)
    locs = item.get("locations") or []
    loc_text = ", ".join(locs[:4]) + (f" +{len(locs) - 4} more" if len(locs) > 4 else "")
    fields = [
        {"name": "Location", "value": (loc_text or "—")[:1024], "inline": True},
        {"name": "Category", "value": category or "—", "inline": True},
    ]
    sponsorship = item.get("sponsorship")
    if sponsorship and sponsorship != "Other":
        fields.append({"name": "Sponsorship", "value": sponsorship, "inline": True})
    embed = {
        "title": f"{item.get('company_name', '?')} — {item.get('title', '?')}"[:256],
        "url": item.get("url") or REPO_URL,
        "color": color,
        "fields": fields,
        "footer": {"text": ", ".join(item.get("terms") or [])},
    }
    if item.get("date_posted"):
        embed["timestamp"] = datetime.fromtimestamp(item["date_posted"], timezone.utc).isoformat()
    return embed


def post_discord(webhook, payload, retries=3):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
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


def send_listings(webhook, items, dry_run):
    """Send cards in batches of 10 (Discord's per-message limit).
    Returns the ids that were delivered."""
    delivered = []
    for i in range(0, len(items), 10):
        batch = items[i:i + 10]
        n = len(items)
        payload = {
            "embeds": [build_embed(x) for x in batch],
            "allowed_mentions": {"parse": []},
        }
        if i == 0:
            payload["content"] = f"🆕 **{n} new internship{'s' if n != 1 else ''}**"
        if dry_run:
            for e in payload["embeds"]:
                print(f"  • {e['title']}  [{e['fields'][0]['value']}]  {e['url']}")
            delivered += [x["id"] for x in batch]
            continue
        if post_discord(webhook, payload):
            delivered += [x["id"] for x in batch]
        else:
            break  # stop; undelivered ones are retried next run
        time.sleep(1)
    return delivered


def send_text(webhook, text, dry_run):
    if dry_run:
        print(text)
        return True
    return post_discord(webhook, {"content": text, "allowed_mentions": {"parse": []}})


# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg["webhook"] and not args.dry_run:
        sys.exit("DISCORD_WEBHOOK_URL is not set (add it as a repository secret).")

    if args.test:
        ok = send_text(cfg["webhook"], "✅ Internship notifier test message — webhook works!", args.dry_run)
        sys.exit(0 if ok else 1)

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"seen": [], "etag": None}

    if args.file:
        with open(args.file) as f:
            listings, etag = json.load(f), None
    else:
        listings, etag = fetch_listings(None if first_run else state.get("etag"))
        if listings is None:
            print("listings.json unchanged since last run.")
            return

    now = time.time()
    seen = set(state["seen"])
    eligible = [x for x in listings if x.get("id") and is_eligible(x, cfg, now)]
    new = [x for x in eligible if x["id"] not in seen]
    new.sort(key=lambda x: x.get("date_posted") or 0)
    print(f"{len(listings)} listings, {len(eligible)} match filters, {len(new)} new.")

    if first_run:
        # Seed silently so we don't blast hundreds of existing roles at you.
        seen.update(x["id"] for x in eligible)
        send_text(
            cfg["webhook"],
            f"👋 Internship notifier is set up. Watching {len(eligible)} current "
            f"matching roles — you'll get a message here when new ones appear.",
            args.dry_run,
        )
    elif new:
        to_send, overflow = new[-cfg["max_per_run"]:], new[:-cfg["max_per_run"]]
        delivered = send_listings(cfg["webhook"], to_send, args.dry_run)
        seen.update(delivered)
        if overflow:
            seen.update(x["id"] for x in overflow)
            send_text(
                cfg["webhook"],
                f"…and {len(overflow)} more older new listings. Full list: <{REPO_URL}>",
                args.dry_run,
            )

    # Forget ids that no longer exist upstream so state.json stays small.
    live_ids = {x.get("id") for x in listings}
    state["seen"] = [i for i in seen if i in live_ids]
    state["etag"] = etag
    if args.dry_run:
        print("(dry run - state not saved)")
    else:
        save_state(state)


if __name__ == "__main__":
    main()
