# TikTok repost watcher

Sends you a notification whenever a TikTok account reposts a video.

TikTok has no official API for reposts, so `tiktok_repost_watcher.py` calls the same
public endpoint the website uses for a profile's **Reposts** tab, remembers which
reposts it has already seen, and notifies you about new ones.

**Requirement:** the account's reposts have to be visible to the public. If you can't
see the Reposts tab on their profile while logged out, this won't work.

## Quick start (on your own computer)

```bash
pip install -r requirements.txt

# ntfy is the easiest option: install the ntfy app on your phone and
# subscribe to a hard-to-guess topic name.
export NTFY_TOPIC=my-secret-repost-alerts-8f3k2

# Check every 10 minutes
python tiktok_repost_watcher.py someusername --interval 600
```

The first check only records the reposts that already exist, so you don't get flooded.
From then on, each new repost sends one notification with the original creator, the
caption, and a link to the video. Pass `--notify-existing` if you want the existing
reposts sent too.

You can watch more than one account: `python tiktok_repost_watcher.py alice bob`.
Leave out `--interval` to check once and exit, for example from cron.

## Notification channels

Set any combination of these environment variables. If none is set, new reposts are
only printed to the terminal.

| Channel | Variables |
| --- | --- |
| [ntfy](https://ntfy.sh) push notifications (phone/desktop) | `NTFY_TOPIC`, optional `NTFY_SERVER` (default `https://ntfy.sh`), `NTFY_TOKEN` |
| Discord | `DISCORD_WEBHOOK_URL` (Server settings → Integrations → Webhooks) |
| Telegram | `TELEGRAM_BOT_TOKEN` (from @BotFather), `TELEGRAM_CHAT_ID` |
| Any HTTP endpoint (Zapier, IFTTT, Home Assistant, …) | `WEBHOOK_URL`; receives JSON with `title`, `message`, `video_id`, `video_url`, `author`, `cover` |

## Running it for free on GitHub Actions

`.github/workflows/watch-reposts.yml` checks every 15 minutes without needing a computer
left on, and commits what it has seen to `state/reposts.json`.

1. In the repository go to **Settings → Secrets and variables → Actions**.
2. Under **Variables**, add `TIKTOK_USERNAMES` (for example `alice` or `alice,bob`).
3. Under **Secrets**, add the variables for your notification channel, e.g. `NTFY_TOPIC`.
4. Open the **Actions** tab, select *Watch TikTok reposts*, and click **Run workflow** once
   to record the existing reposts. Scheduled runs take over from there.

GitHub can run scheduled workflows late when it's busy, so notifications may arrive a
few minutes after the repost. If TikTok starts blocking GitHub's servers, run the script
on your own machine instead.

## Options

```
python tiktok_repost_watcher.py [USERNAME ...] [--interval SECONDS] [--state PATH]
                                [--notify-existing] [-v]
```

- `--state` — where seen reposts are stored (default `state/reposts.json`, or `$STATE_FILE`)
- `--interval` — keep running and check every N seconds (minimum 60)
- Usernames can also come from the comma-separated `TIKTOK_USERNAMES` variable

## Limitations

- This relies on an undocumented TikTok endpoint. If TikTok changes it, the script will
  log an error, exit with a non-zero status, and keep its saved state until it works again.
- Only the 30 most recent reposts are checked each time, so keep the interval short
  enough that an account can't repost more than 30 videos between checks.
- Checking too often can get you rate-limited. Every 10–15 minutes is plenty.

## Tests

```bash
pip install pytest
pytest
```
