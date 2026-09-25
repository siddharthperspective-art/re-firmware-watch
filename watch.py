#!/usr/bin/env python3
"""Watch for Royal Enfield Himalayan 450 Tripper Dash firmware releases.

Royal Enfield publishes no firmware feed or release notes: dash updates ship
over the air through the Royal Enfield app (Connected devices -> Infotainment
settings -> Software update). So this polls the signals that move when a
release lands and notifies about anything new:

  1. Royal Enfield App version + release notes on the App Store
     (same app/bundle id as the Android app, com.royalenfield.reprime).
  2. Google News articles about Himalayan 450 / Tripper Dash updates.
  3. Reddit posts in r/Himalayan450 and r/royalenfield about updates.

State lives next to this file in state.json. Each source's first successful
check only records what already exists, so later runs notify about genuinely
new items.

Runs hourly on GitHub Actions (.github/workflows/watch.yml, started by a
cron-job.org trigger), which commits state.json back to the repo. Settings
come from config.json and/or the
WATCH_CHANNELS and NTFY_TOPIC environment variables.

Usage:
  watch.py              check all sources, notify, save state
  watch.py --dry-run    print what would be notified; don't notify or save
  watch.py --test       send a test notification through every channel
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "watch.log"

USER_AGENT = "Mozilla/5.0 (re-firmware-watch; personal update checker)"
TIMEOUT_S = 20
MAX_NOTIFICATIONS = 5   # per run; the rest are summarised in one notification
MAX_SEEN = 1000         # remembered item ids

# (label, App Store track id, storefront country)
APP_STORE_APPS = [
    ("Royal Enfield App", 1472963660, "in"),
]

NEWS_QUERY = ('("Himalayan 450" OR "Tripper Dash" OR "Royal Enfield app") '
              '(firmware OR FOTA OR OTA OR "software update" OR update)')
NEWS_URL = ("https://news.google.com/rss/search?q={}&hl=en-IN&gl=IN&ceid=IN:en"
            .format(urllib.parse.quote(NEWS_QUERY)))

REDDIT_QUERY = 'firmware OR FOTA OR "software update" OR "dash update" OR "tripper update"'
REDDIT_URL = ("https://www.reddit.com/r/Himalayan450+royalenfield/search.rss"
              "?q={}&restrict_sr=1&sort=new&t=month"
              .format(urllib.parse.quote(REDDIT_QUERY)))

# Google News matches loosely, so its titles must name the bike/dash AND an update.
NEWS_TOPIC_RE = re.compile(r"himalayan|tripper|royal enfield app|\bre app\b|guerrilla 450", re.I)
UPDATE_RE = re.compile(
    r"firmware|\bf?ota\b|over[- ]the[- ]air|software"
    r"|(dash|tripper|app|navigation|cluster|tft)\W+(\w+\W+){0,3}updat"
    r"|updat\w*\W+(\w+\W+){0,3}(dash|tripper|app|navigation|cluster|tft)",
    re.I)
# Reddit search already matched the keywords (often in the body), so only drop
# titles that are clearly unrelated.
REDDIT_TITLE_RE = re.compile(
    r"tripper|dash|firmware|\bf?ota\b|software|updat|cluster|tft|version|\bv?\d+\.\d+", re.I)

SOURCE_COUNT = len(APP_STORE_APPS) + 2   # App Store apps + Google News + Reddit
IST = timezone(timedelta(hours=5, minutes=30), "IST")
QUIET_PRIORITY = 2   # ntfy "low": listed in the notification shade, no sound or vibration

DEFAULT_CONFIG = {
    # "macos" = Notification Centre on this Mac; "ntfy" = push to phone via ntfy.sh
    "channels": ["macos"],
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    # Also send a low-priority "Nothing new found" message after every quiet check.
    "notify_when_quiet": False,
}


def log(msg):
    line = "{} {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read()


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


# ── Sources ──────────────────────────────────────────────────────────────
# Each returns (key, items); an item is a dict with id/title/body/url.

def check_app_store(label, track_id, country):
    data = json.loads(fetch(
        "https://itunes.apple.com/lookup?id={}&country={}".format(track_id, country)))
    app = data["results"][0]
    version = app["version"]
    released = app.get("currentVersionReleaseDate", "")[:10]
    notes = (app.get("releaseNotes") or "").strip() or "No release notes."
    return "app:{}".format(track_id), [{
        "id": "app:{}:{}".format(track_id, version),
        "title": "{} {} released".format(label, version),
        "body": "{} - {}".format(released, notes),
        "url": app.get("trackViewUrl", ""),
        "version": version,
    }]


def check_google_news():
    root = ET.fromstring(fetch(NEWS_URL))
    items = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        if not (NEWS_TOPIC_RE.search(title) and UPDATE_RE.search(title)):
            continue
        items.append({
            "id": "news:" + (it.findtext("guid") or it.findtext("link") or title),
            "title": "News: " + title,
            "body": (it.findtext("pubDate") or "")[:16],
            "url": it.findtext("link") or "",
        })
    return "news", items


def check_reddit():
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(fetch(REDDIT_URL))
    items = []
    for e in root.findall("a:entry", ns):
        title = e.findtext("a:title", namespaces=ns) or ""
        if not REDDIT_TITLE_RE.search(title):
            continue
        cat = e.find("a:category", ns)
        sub = cat.get("term") if cat is not None else "reddit"
        link = e.find("a:link", ns)
        items.append({
            "id": "reddit:" + (e.findtext("a:id", namespaces=ns) or title),
            "title": "r/{}: {}".format(sub, title),
            "body": (e.findtext("a:updated", namespaces=ns) or "")[:10],
            "url": link.get("href") if link is not None else "",
        })
    return "reddit", items


def run_sources(failed):
    """Yield (key, items) per source; a failing source is logged, named in failed, and skipped."""
    checks = [("App Store", lambda a=a: check_app_store(*a)) for a in APP_STORE_APPS]
    checks += [("Google News", check_google_news), ("Reddit", check_reddit)]
    for name, check in checks:
        try:
            yield check()
        except Exception as e:  # network/parse failure: keep the other sources going
            failed.append(name)
            log("WARN source failed: {}: {}: {}".format(name, type(e).__name__, e))


def quiet_summary(current_versions, failed):
    """Title, body, and ntfy tags for the message sent after a quiet check."""
    checked = "Checked at {} IST.".format(datetime.now(IST).strftime("%H:%M"))
    if len(failed) == SOURCE_COUNT:
        return ("Check couldn't run",
                "{} No source could be reached; retrying next hour.".format(checked), ["warning"])
    parts = [checked]
    if current_versions:
        parts.append("{} is unchanged.".format(", ".join(current_versions)))
    parts.append("Unavailable this time: {}.".format(", ".join(failed)) if failed
                 else "All sources checked.")
    return "Nothing new found", " ".join(parts), ["white_check_mark"]


# ── Notification channels ───────────────────────────────────────────────

def notify_macos(title, body, url):
    # argv passing avoids AppleScript quoting issues with arbitrary titles.
    subprocess.run(
        ["osascript",
         "-e", "on run argv",
         "-e", "display notification (item 2 of argv) with title \"Tripper Dash watch\" "
               "subtitle (item 1 of argv)",
         "-e", "end run",
         title[:120], body[:240]],
        check=True, capture_output=True)


def notify_ntfy(title, body, url, config, priority=None, tags=None):
    topic = config.get("ntfy_topic", "").strip()
    if not topic:
        raise ValueError("ntfy channel enabled but ntfy_topic is empty in config.json")
    payload = {"topic": topic, "title": title[:250], "message": body[:1000] or " ",
               "tags": tags or ["motorcycle"]}
    if priority:
        payload["priority"] = priority
    if url:
        payload["click"] = url
    req = urllib.request.Request(
        config.get("ntfy_server", "https://ntfy.sh").rstrip("/"),
        data=json.dumps(payload).encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST")
    urllib.request.urlopen(req, timeout=TIMEOUT_S).read()


def notify(title, body, url, config, priority=None, tags=None):
    for channel in config.get("channels", []):
        try:
            if channel == "macos":
                notify_macos(title, body, url)
            elif channel == "ntfy":
                notify_ntfy(title, body, url, config, priority, tags)
            else:
                log("WARN unknown channel '{}'".format(channel))
        except Exception as e:
            log("WARN notify via {} failed: {}".format(channel, e))


# ── Main ─────────────────────────────────────────────────────────────────

def main(argv):
    dry_run = "--dry-run" in argv
    config = dict(DEFAULT_CONFIG, **load_json(CONFIG_FILE, {}))
    # Environment overrides, used by the GitHub Actions workflow (topic is a secret there).
    if os.environ.get("WATCH_CHANNELS"):
        config["channels"] = [c.strip() for c in os.environ["WATCH_CHANNELS"].split(",") if c.strip()]
    if os.environ.get("NTFY_TOPIC"):
        config["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    if os.environ.get("NOTIFY_WHEN_QUIET"):
        config["notify_when_quiet"] = os.environ["NOTIFY_WHEN_QUIET"].lower() in ("1", "true", "yes")

    if "--test" in argv:
        notify("Test notification", "Firmware watch can reach you on: "
               + ", ".join(config["channels"]), "", config)
        log("Sent test notification via {}".format(config["channels"]))
        return 0

    state = load_json(STATE_FILE, {"app_versions": {}, "seen": {}, "initialised": False})
    # Sources whose existing items have been recorded. A source is baselined silently
    # the first time it succeeds (not just on the first run), so one that was down on
    # day one doesn't flood notifications with old items later.
    baselined = state.setdefault("baselined", [])
    first_run = not state.get("initialised")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_items = []
    current_versions = []
    failed = []

    for key, items in run_sources(failed):
        source_new = []
        if key.startswith("app:"):
            item = items[0]
            current_versions.append(item["title"].replace(" released", ""))
            old = state["app_versions"].get(key)
            if old != item["version"]:
                if old is not None:
                    item["body"] = "was {}. {}".format(old, item["body"])
                source_new.append(item)
                state["app_versions"][key] = item["version"]
        else:
            for item in items:
                if item["id"] not in state["seen"]:
                    source_new.append(item)
                    state["seen"][item["id"]] = now
        if key in baselined:
            new_items += source_new
        else:
            baselined.append(key)
            if dry_run:
                for it in source_new:
                    print("BASELINE (no notify): {}".format(it["title"]))
            else:
                log("Baseline for {}: recorded {} existing item(s)".format(key, len(source_new)))

    # Keep the most recently seen ids only.
    if len(state["seen"]) > MAX_SEEN:
        keep = sorted(state["seen"].items(), key=lambda kv: kv[1])[-MAX_SEEN:]
        state["seen"] = dict(keep)

    if dry_run:
        for it in new_items:
            print("WOULD NOTIFY: {} | {} | {}".format(it["title"], it["body"][:80], it["url"][:80]))
        print("{} new item(s); first_run={}; state not saved".format(len(new_items), first_run))
        return 0

    if first_run:
        notify("Watch is active",
               "Current: {}. You'll be notified about new app releases and firmware "
               "news/posts.".format(", ".join(current_versions) or "n/a"), "", config)
    for it in new_items[:MAX_NOTIFICATIONS]:
        notify(it["title"], it["body"], it["url"], config)
        log("NOTIFIED {} | {}".format(it["title"], it["url"]))
    if len(new_items) > MAX_NOTIFICATIONS:
        extra = len(new_items) - MAX_NOTIFICATIONS
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):  # link the Actions run log, where the full list is
            run_url = "{}/{}/actions/runs/{}".format(
                os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                os.environ.get("GITHUB_REPOSITORY", ""), os.environ["GITHUB_RUN_ID"])
        notify("{} more update item(s)".format(extra),
               "Full list in the run log." if run_url else "See {}".format(LOG_FILE),
               run_url, config)
        for it in new_items[MAX_NOTIFICATIONS:]:
            log("NEW (not notified) {} | {}".format(it["title"], it["url"]))
    if not new_items:
        log("No new items")
        # First run already sent "Watch is active"; after that, confirm every quiet check.
        if config.get("notify_when_quiet") and not first_run:
            title, body, tags = quiet_summary(current_versions, failed)
            notify(title, body, "", config, priority=QUIET_PRIORITY, tags=tags)
            log("NOTIFIED {} | {}".format(title, body))

    state["initialised"] = True
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
