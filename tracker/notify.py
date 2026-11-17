"""Telegram + email notifications. Both are optional and configured via env vars."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


# ---------------------------------------------------------------- Telegram

def tg_enabled() -> bool:
    return bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))


def _tg(method: str, **params):
    token = _env("TELEGRAM_BOT_TOKEN")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=20)
    r.raise_for_status()
    return r.json().get("result")


def tg_send(text: str, chat_id: str | None = None) -> None:
    if not _env("TELEGRAM_BOT_TOKEN"):
        return
    chat = chat_id or _env("TELEGRAM_CHAT_ID")
    if not chat:
        return
    try:
        _tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
            disable_web_page_preview=True)
    except requests.RequestException as e:
        print(f"[telegram] send failed: {e}")


def tg_updates(offset: int) -> list[dict]:
    if not _env("TELEGRAM_BOT_TOKEN"):
        return []
    try:
        return _tg("getUpdates", offset=offset, timeout=0, allowed_updates=["message"]) or []
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 409:
            return []  # a webhook is active; messages arrive via data/inbox instead
        print(f"[telegram] getUpdates failed: {e}")
        return []
    except requests.RequestException as e:
        print(f"[telegram] getUpdates failed: {e}")
        return []


def tg_allowed_chat() -> str:
    return _env("TELEGRAM_CHAT_ID")


# ---------------------------------------------------------------- Email

def email_enabled() -> bool:
    return all(_env(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))


def email_send(subject: str, body_html: str) -> None:
    if not email_enabled():
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _env("EMAIL_FROM") or _env("SMTP_USER")
    msg["To"] = _env("EMAIL_TO")
    msg.set_content("Open this email in an HTML-capable client.")
    msg.add_alternative(body_html, subtype="html")
    port = int(_env("SMTP_PORT") or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(_env("SMTP_HOST"), port, timeout=30) as s:
                s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
                s.send_message(msg)
        else:
            with smtplib.SMTP(_env("SMTP_HOST"), port, timeout=30) as s:
                s.starttls()
                s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        print(f"[email] send failed: {e}")
