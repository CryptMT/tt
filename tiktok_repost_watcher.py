#!/usr/bin/env python3
"""Watch one or more TikTok accounts and send a notification whenever they repost a video.

TikTok has no official API for reposts, so this uses the same public endpoint the
website calls when you open the "Reposts" tab of a profile. The account's reposts
must be visible to the public for this to work.

Usage:
    python tiktok_repost_watcher.py USERNAME [USERNAME ...]            # check once
    python tiktok_repost_watcher.py USERNAME --interval 600            # check every 10 min

Notification channels are configured with environment variables (see README.md).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("repost-watcher")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PROFILE_URL = "https://www.tiktok.com/@{username}"
REPOST_API = "https://www.tiktok.com/api/repost/item_list/"
# How many repost IDs to remember per account. Must comfortably exceed one page.
MAX_REMEMBERED = 500


class WatcherError(Exception):
    pass


@dataclass(frozen=True)
class Repost:
    id: str
    author: str
    desc: str
    cover: str

    @property
    def url(self) -> str:
        return f"https://www.tiktok.com/@{self.author}/video/{self.id}"


# --------------------------------------------------------------------------- TikTok


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        }
    )
    return s


def resolve_sec_uid(session: requests.Session, username: str) -> str:
    """Look up the account's secUid, the stable ID the reposts API is keyed on."""
    resp = session.get(PROFILE_URL.format(username=username), timeout=30)
    resp.raise_for_status()
    match = re.search(
        r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        resp.text,
        re.S,
    )
    if match:
        try:
            data = json.loads(match.group(1))
            user = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]["user"]
            if user.get("secUid"):
                return user["secUid"]
        except (KeyError, TypeError, json.JSONDecodeError):
            pass
    # Fallback: the first secUid on the page is the profile owner's.
    match = re.search(r'"secUid":"([^"]+)"', resp.text)
    if match:
        return match.group(1)
    raise WatcherError(f"could not find @{username} (does the account exist?)")


def fetch_reposts(session: requests.Session, sec_uid: str, count: int = 30) -> list[Repost]:
    """Return the account's most recent reposts, newest first."""
    params = {"aid": "1988", "count": str(count), "cursor": "0", "secUid": sec_uid}
    resp = session.get(REPOST_API, params=params, timeout=30)
    resp.raise_for_status()
    if not resp.content:
        raise WatcherError("empty response from reposts API (probably rate limited)")
    data = resp.json()
    if data.get("statusCode", 0) != 0:
        raise WatcherError(f"reposts API error {data.get('statusCode')}: {data.get('status_msg')}")
    reposts = []
    for item in data.get("itemList") or []:
        reposts.append(
            Repost(
                id=str(item["id"]),
                author=(item.get("author") or {}).get("uniqueId", ""),
                desc=item.get("desc", ""),
                cover=(item.get("video") or {}).get("cover", ""),
            )
        )
    return reposts


# --------------------------------------------------------------------------- State


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def find_new(reposts: list[Repost], seen_ids: list[str]) -> list[Repost]:
    seen = set(seen_ids)
    return [r for r in reposts if r.id not in seen]


def remember(seen_ids: list[str], reposts: list[Repost]) -> list[str]:
    """Prepend newly seen IDs, keeping the list bounded and newest first."""
    merged = [r.id for r in reposts] + [i for i in seen_ids if i not in {r.id for r in reposts}]
    return merged[:MAX_REMEMBERED]


# --------------------------------------------------------------------------- Notifications


def notify(username: str, repost: Repost) -> None:
    title = f"@{username} reposted a video"
    by = f" by @{repost.author}" if repost.author else ""
    desc = repost.desc.strip()
    if len(desc) > 200:
        desc = desc[:197] + "..."
    body = f"Video{by}: {desc}\n{repost.url}" if desc else f"Video{by}\n{repost.url}"

    sent = False
    for name, fn in NOTIFIERS:
        if not fn.enabled():
            continue
        try:
            fn.send(title, body, repost)
            sent = True
        except Exception as exc:  # one broken channel shouldn't block the others
            log.error("%s notification failed: %s", name, exc)
    print(f"[NEW REPOST] {title}\n  {body.replace(chr(10), chr(10) + '  ')}", flush=True)
    if not sent:
        log.debug("no notification channels configured; printed to stdout only")


class _Ntfy:
    @staticmethod
    def enabled() -> bool:
        return bool(os.environ.get("NTFY_TOPIC"))

    @staticmethod
    def send(title: str, body: str, repost: Repost) -> None:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("utf-8"), "Click": repost.url, "Tags": "repeat"}
        if repost.cover:
            headers["Attach"] = repost.cover
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        r = requests.post(f"{server}/{os.environ['NTFY_TOPIC']}", data=body.encode(), headers=headers, timeout=30)
        r.raise_for_status()


class _Discord:
    @staticmethod
    def enabled() -> bool:
        return bool(os.environ.get("DISCORD_WEBHOOK_URL"))

    @staticmethod
    def send(title: str, body: str, repost: Repost) -> None:
        embed = {"title": title, "description": body, "url": repost.url}
        if repost.cover:
            embed["thumbnail"] = {"url": repost.cover}
        r = requests.post(os.environ["DISCORD_WEBHOOK_URL"], json={"embeds": [embed]}, timeout=30)
        r.raise_for_status()


class _Telegram:
    @staticmethod
    def enabled() -> bool:
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))

    @staticmethod
    def send(title: str, body: str, repost: Repost) -> None:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
            json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": f"{title}\n\n{body}"},
            timeout=30,
        )
        r.raise_for_status()


class _Webhook:
    @staticmethod
    def enabled() -> bool:
        return bool(os.environ.get("WEBHOOK_URL"))

    @staticmethod
    def send(title: str, body: str, repost: Repost) -> None:
        payload = {"title": title, "message": body, "video_id": repost.id,
                   "video_url": repost.url, "author": repost.author, "cover": repost.cover}
        r = requests.post(os.environ["WEBHOOK_URL"], json=payload, timeout=30)
        r.raise_for_status()


NOTIFIERS = [("ntfy", _Ntfy), ("discord", _Discord), ("telegram", _Telegram), ("webhook", _Webhook)]


# --------------------------------------------------------------------------- Main loop


def check_account(session: requests.Session, username: str, state: dict, notify_existing: bool) -> int:
    """Check one account, send notifications, update state in place. Returns # of new reposts."""
    entry = state.setdefault(username, {})
    if not entry.get("sec_uid"):
        entry["sec_uid"] = resolve_sec_uid(session, username)

    reposts = fetch_reposts(session, entry["sec_uid"])
    first_run = "seen" not in entry
    seen = entry.get("seen", [])

    if first_run and not notify_existing:
        log.info("@%s: first run, recorded %d existing reposts (no notifications sent)", username, len(reposts))
        new = []
    else:
        new = find_new(reposts, seen)
        # Notify oldest first so they arrive in the order they were reposted.
        for repost in reversed(new):
            notify(username, repost)
        log.info("@%s: %d new repost(s)", username, len(new))

    entry["seen"] = remember(seen, reposts)
    entry["last_checked"] = int(time.time())
    return len(new)


def run_once(usernames: list[str], state_path: Path, notify_existing: bool) -> bool:
    state = load_state(state_path)
    session = make_session()
    ok = True
    for username in usernames:
        try:
            check_account(session, username, state, notify_existing)
        except (requests.RequestException, WatcherError, ValueError) as exc:
            ok = False
            log.error("@%s: check failed: %s", username, exc)
        # Save after each account so a crash later doesn't cause duplicate notifications.
        save_state(state_path, state)
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("usernames", nargs="*", help="TikTok usernames to watch (with or without @). "
                        "Defaults to the comma-separated TIKTOK_USERNAMES env var.")
    parser.add_argument("--state", default=os.environ.get("STATE_FILE", "state/reposts.json"),
                        help="where to remember already-seen reposts (default: %(default)s)")
    parser.add_argument("--interval", type=int, default=0,
                        help="keep running and check every N seconds (default: check once and exit)")
    parser.add_argument("--notify-existing", action="store_true",
                        help="on the first run, notify about reposts that already exist")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    usernames = args.usernames or [u for u in os.environ.get("TIKTOK_USERNAMES", "").split(",") if u.strip()]
    usernames = [u.strip().lstrip("@") for u in usernames]
    if not usernames:
        parser.error("give at least one username (or set TIKTOK_USERNAMES)")
    if args.interval and args.interval < 60:
        parser.error("--interval must be at least 60 seconds to avoid getting rate limited")

    state_path = Path(args.state)
    if not args.interval:
        return 0 if run_once(usernames, state_path, args.notify_existing) else 1

    while True:
        run_once(usernames, state_path, args.notify_existing)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
