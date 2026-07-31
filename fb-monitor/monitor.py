"""Facebook Page monitor.

Polls an RSS feed of a public Facebook Page, notifies on posts matching
configured keywords, and remembers what it has already seen.

Runs in two modes:
  Single shot   LOOP_DURATION_SECONDS unset or 0 - one check, then exit.
  Polling loop  LOOP_DURATION_SECONDS=270 - keeps checking every
                LOOP_INTERVAL_SECONDS until the budget is spent.

The loop exists because GitHub Actions cannot schedule more often than every
5 minutes. A job that polls internally for ~4.5 minutes, launched by a */5
cron, gives near-continuous 60-second coverage at the same billed cost.

Required env:
  FEED_URL   RSS feed URL for the Facebook Page
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import feedparser
import requests

from notifiers import notify_all

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
USER_AGENT = "fb-monitor/1.1 (+github actions)"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen": [], "bootstrapped": False, "etag": None, "last_modified": None}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARN: could not read state ({e}); starting fresh, notifications suppressed this run")
        data = {}
    data.setdefault("seen", [])
    data.setdefault("bootstrapped", False)
    data.setdefault("etag", None)
    data.setdefault("last_modified", None)
    return data


def save_state(path: Path, state: dict, max_entries: int) -> None:
    state["seen"] = state["seen"][-max_entries:]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class FeedUnchanged(Exception):
    """Server returned 304 - nothing new since our last conditional request."""


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry after {retry_after}s")


def fetch_feed(url: str, state: dict, attempts: int = 3):
    """Conditional GET with retries.

    Sending If-None-Match / If-Modified-Since matters a lot when polling every
    60 seconds: unchanged feeds come back as an empty 304 instead of a full
    body, which is what keeps free-tier bridges from rate-limiting you.
    """
    last_error = None
    for i in range(attempts):
        try:
            headers = {"User-Agent": USER_AGENT}
            if state.get("etag"):
                headers["If-None-Match"] = state["etag"]
            if state.get("last_modified"):
                headers["If-Modified-Since"] = state["last_modified"]

            r = requests.get(url, headers=headers, timeout=30)

            if r.status_code == 304:
                raise FeedUnchanged()
            if r.status_code == 429:
                raise RateLimited(int(r.headers.get("Retry-After", 120)))
            r.raise_for_status()

            parsed = feedparser.parse(r.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"malformed feed: {parsed.bozo_exception}")

            state["etag"] = r.headers.get("ETag")
            state["last_modified"] = r.headers.get("Last-Modified")
            return parsed
        except (FeedUnchanged, RateLimited):
            raise
        except Exception as e:
            last_error = e
            print(f"WARN: feed fetch attempt {i + 1}/{attempts} failed: {e}")
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
    raise RuntimeError(f"could not fetch feed after {attempts} attempts: {last_error}")


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize(text: str) -> str:
    """NFKC folds full-width chars to half-width so ｎａｔｏｒｉ matches natori."""
    return unicodedata.normalize("NFKC", text or "").casefold()


def build_matchers(keywords: list) -> list:
    """ASCII keywords get word boundaries; CJK gets substring matching.

    \\b is meaningless between Japanese characters (there are no word breaks),
    so applying it to なとり would silently never match.
    """
    matchers = []
    for kw in keywords:
        norm_kw = normalize(kw)
        if norm_kw.isascii():
            pattern = re.compile(rf"(?<!\w){re.escape(norm_kw)}(?!\w)")
            matchers.append((kw, lambda text, p=pattern: bool(p.search(text))))
        else:
            matchers.append((kw, lambda text, k=norm_kw: k in text))
    return matchers


def entry_id(entry) -> str:
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if value:
            return str(value)
    seed = f"{entry.get('title', '')}|{entry.get('published', '')}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def extract_post(entry) -> dict:
    body = ""
    if entry.get("content"):
        body = entry["content"][0].get("value", "")
    if not body:
        body = entry.get("summary", "") or entry.get("description", "")
    return {
        "id": entry_id(entry),
        "title": strip_html(entry.get("title", "")),
        "body": strip_html(body),
        "link": entry.get("link", ""),
        "published": entry.get("published", "") or entry.get("updated", ""),
        "matched": [],
    }


def check_once(config, state, matchers, feed_url, notify: bool) -> int:
    """One poll. Returns number of notifications sent."""
    feed = fetch_feed(feed_url, state)
    seen = set(state["seen"])
    match_mode = config.get("match_mode", "any")

    matches = []
    for entry in feed.entries:
        post = extract_post(entry)
        if post["id"] in seen:
            continue

        seen.add(post["id"])
        state["seen"].append(post["id"])

        haystack = normalize(f"{post['title']}\n{post['body']}")
        post["matched"] = [kw for kw, test in matchers if test(haystack)]

        hit = (
            len(post["matched"]) == len(matchers)
            if match_mode == "all"
            else bool(post["matched"])
        )
        if hit:
            matches.append(post)

    if not matches:
        return 0
    if not notify:
        print(f"Bootstrap: recorded {len(matches)} existing matches without notifying.")
        return 0

    cap = config.get("max_notifications_per_run", 10)
    if len(matches) > cap:
        print(f"WARN: {len(matches)} matches, capping notifications at {cap}")

    sent = 0
    for post in reversed(matches[:cap]):  # oldest first
        print(f"Notifying: {post['title'][:80]!r} (matched: {post['matched']})")
        if notify_all(post):
            sent += 1
    return sent


def main() -> int:
    feed_url = os.environ.get("FEED_URL", "").strip()
    if not feed_url:
        print("ERROR: FEED_URL is not set")
        return 1

    config = load_config()
    state_path = ROOT / config["state_file"]
    state = load_state(state_path)
    matchers = build_matchers(config["keywords"])

    duration = int(os.environ.get("LOOP_DURATION_SECONDS", "0"))
    interval = max(30, int(os.environ.get("LOOP_INTERVAL_SECONDS", "60")))
    max_entries = config.get("state_max_entries", 500)

    notify = state["bootstrapped"]
    if not notify:
        print("First run: recording current posts as seen, no notifications sent.")

    deadline = time.monotonic() + duration
    polls = 0
    total_sent = 0
    fatal = None

    while True:
        polls += 1
        try:
            total_sent += check_once(config, state, matchers, feed_url, notify)
            notify = True
        except FeedUnchanged:
            print(f"[poll {polls}] 304 not modified")
        except RateLimited as e:
            print(f"[poll {polls}] rate limited; backing off {e.retry_after}s")
            time.sleep(min(e.retry_after, max(0, deadline - time.monotonic())))
        except Exception as e:
            # Save what we have before deciding whether to abort.
            fatal = e
            print(f"[poll {polls}] ERROR: {e}")
            break

        state["bootstrapped"] = True
        save_state(state_path, state, max_entries)

        remaining = deadline - time.monotonic()
        if remaining < interval:
            break
        time.sleep(interval)

    state["bootstrapped"] = True
    save_state(state_path, state, max_entries)
    print(f"Done. {polls} polls, {total_sent} notified, {len(state['seen'])} ids tracked.")

    if fatal is not None:
        print("Failing the job so GitHub emails you about it.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
