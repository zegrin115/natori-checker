"""Notification channels: Discord webhook, Telegram bot, SMTP email.

Every sender is best-effort and independent: if Discord is down, Telegram and
email still go out. Each returns True on success, False on failure, and never
raises. Channels with missing credentials are silently skipped so you can run
with only the ones you've configured.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

import requests

TIMEOUT = 20


def env(name: str, default: str = "") -> str:
    """Read an env var, treating empty as missing.

    GitHub Actions sets `FOO: ${{ secrets.FOO }}` to an empty string when the
    secret doesn't exist, rather than leaving it unset. os.environ.get's
    default only applies to absent keys, so an unconfigured secret would slip
    through as "" and blow up on int(""). Anything falsy falls back here.
    """
    return (os.environ.get(name) or default).strip()


def _log(channel: str, ok: bool, detail: str = "") -> bool:
    status = "sent" if ok else "skipped"
    print(f"  [{channel}] {status}{(' - ' + detail) if detail else ''}")
    return ok


def send_discord(post: dict) -> bool:
    url = env("DISCORD_WEBHOOK_URL")
    if not url:
        return _log("discord", False, "not configured")

    body = post["body"][:1500] or "(no text content)"
    payload = {
        "content": f"New post matching **{', '.join(post['matched'])}**",
        "embeds": [
            {
                "title": post["title"][:250] or "Facebook post",
                "url": post["link"],
                "description": body,
                "footer": {"text": post["published"] or ""},
                "color": 0x1877F2,
            }
        ],
    }
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return _log("discord", True)
    except Exception as e:
        return _log("discord", False, f"ERROR: {e}")


def send_telegram(post: dict) -> bool:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return _log("telegram", False, "not configured")

    text = (
        f"New post matching: {', '.join(post['matched'])}\n\n"
        f"{post['title']}\n\n"
        f"{post['body'][:2000]}\n\n"
        f"{post['link']}"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": False,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return _log("telegram", True)
    except Exception as e:
        return _log("telegram", False, f"ERROR: {e}")


def send_email(post: dict) -> bool:
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    if not user or not password:
        return _log("email", False, "not configured")

    host = env("SMTP_HOST", "smtp.gmail.com")
    port = int(env("SMTP_PORT", "465"))
    to_addr = env("EMAIL_TO") or user

    msg = EmailMessage()
    msg["Subject"] = f"[FB Monitor] {post['title'][:120] or 'New post'}"
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(
        f"Matched keywords: {', '.join(post['matched'])}\n"
        f"Published: {post['published']}\n\n"
        f"{post['body']}\n\n"
        f"Link: {post['link']}\n"
    )

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=TIMEOUT) as s:
            s.login(user, password)
            s.send_message(msg)
        return _log("email", True)
    except Exception as e:
        return _log("email", False, f"ERROR: {e}")


def notify_all(post: dict) -> bool:
    """Fan out to every channel. True if at least one delivery succeeded."""
    results = [send_discord(post), send_telegram(post), send_email(post)]
    return any(results)
