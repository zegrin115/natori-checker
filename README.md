# Facebook Page Monitor

Polls a public Facebook Page via RSS every 20 minutes and sends Discord, Telegram, and email notifications when a new post matches your keywords.

Current keywords: `natori`, `なとり` (edit in `config.json`).

## Setup

### 1. Create the repo

Make it **private**. Push these files to `main`.

### 2. Create the RSS feed

Facebook blocks datacenter IPs, so GitHub runners can't scrape the Page directly. Use a bridge service:

- Go to [rss.app](https://rss.app) (free tier covers 1–2 feeds)
- Paste `https://www.facebook.com/share/1EFwsdqtU9/`
- Copy the generated `.xml` feed URL

Alternatives: FetchRSS, or self-hosted [RSS-Bridge](https://github.com/RSS-Bridge/rss-bridge) if you'd rather not depend on a third party.

### 3. Discord webhook

Server Settings → Integrations → Webhooks → New Webhook → pick a channel → Copy Webhook URL.

### 4. Telegram bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts, copy the token.
2. Send any message to your new bot (bots can't message you first).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `result[0].message.chat.id`.

### 5. Gmail app password

Requires 2FA enabled on the account. Google Account → Security → 2-Step Verification → App passwords → generate one for "Mail". Use the 16-character password, **not** your normal Gmail password.

### 6. Add repository secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `FEED_URL` | RSS feed URL from step 2 |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | 16-char app password |
| `EMAIL_TO` | where to send alerts |

Any channel with missing secrets is skipped, not failed — you can add Telegram later without touching the code.

### 7. First run

Actions → Facebook Monitor → Run workflow.

**The first run sends nothing on purpose.** It records the Page's current posts as "already seen" so you don't get 20 notifications for old content. Everything after that is live.

## How it works

1. Fetches the RSS feed (3 retries with backoff; hard-fails the Action if the feed is unreachable, so GitHub emails you)
2. Skips post IDs already in `state/seen.json`
3. Matches keywords against title + body, Unicode-normalized (NFKC + casefold), so full-width `ＮＡＴＯＲＩ` matches too
4. ASCII keywords use word boundaries — `natori` won't fire on `natorious`. Japanese keywords use substring matching, because `\b` doesn't work between CJK characters
5. Sends to all three channels independently; one failing doesn't block the others
6. Commits updated `state/seen.json` back to the repo (last 500 IDs)

## Scan frequency

Configured for the fastest setup GitHub actually supports:

- **Cron `2-59/5`** — 5 minutes is GitHub's hard minimum. Shorter expressions parse fine but never fire. The `2` offset avoids the top-of-hour congestion window, where queue delays are worst.
- **In-job polling loop** — each job polls for 4.5 minutes at 60-second intervals (`LOOP_DURATION_SECONDS` / `LOOP_INTERVAL_SECONDS`), so a 5-minute cron produces continuous **60-second** coverage rather than 5-minute gaps.
- **Conditional GET** — unchanged feeds return an empty `304` instead of a full body. This is what makes 60-second polling safe against a free-tier bridge's rate limits. A `429` triggers automatic backoff.

### This requires a public repository

Public repos get unlimited Actions minutes. Private repos on the free plan get ~2,000 minutes/month, and **every job is billed rounded up to a whole minute**:

| Schedule | Jobs/month | Billed minutes | Fits in free private? |
|---|---|---|---|
| `*/20`, single poll | ~2,160 | ~2,160 | No — already over |
| `*/5`, 4.5-min loop | ~8,640 | ~43,000 | Not remotely |

So: **make the repo public** for this schedule, or keep it private and drop back to roughly `*/30` single-poll. There's no third option on the free tier. Your secrets stay encrypted either way; going public only exposes `config.json` keywords and this README.

### The real bottleneck is upstream

None of the above beats your RSS bridge's own refresh rate. If rss.app's free tier only re-crawls the Page hourly, 60-second polling changes nothing — you'll just see the post the moment the bridge does. **Check your bridge's refresh interval before assuming the cron is the limit.** If it's hourly, either upgrade that plan or self-host RSS-Bridge, which fetches on demand.

Realistic end-to-end latency: **1–2 minutes** with an on-demand bridge, **up to an hour** on a free hourly-refresh bridge, plus occasional GitHub queue delays of 5–30 minutes under load.

## Tuning

- **Keywords**: edit `config.json`. Set `match_mode` to `"all"` to require every keyword instead of any.
- **Slower/cheaper**: set `LOOP_DURATION_SECONDS: "0"` for one poll per job, and widen the cron.
- **Reset**: delete `state/seen.json` to re-bootstrap (the next run will re-silence and re-seed).

## Known limits

- **60-day auto-disable.** GitHub disables scheduled workflows after 60 days of repository inactivity. The bot's own state commits do not reliably reset this timer — push a manual commit every couple of months, or re-enable from the Actions tab when it happens.
- **Silent staleness.** If the bridge stops refreshing, the feed still parses and the monitor stays green while going quiet. Spot-check monthly.
- **Image-only posts** match only if the caption contains a keyword.
- **GitHub's ToS** scopes Actions to work related to the repository's own project. A personal notification cron is a widely-used gray area, not an explicitly sanctioned one. Worth knowing before you scale it up.
