"""WhatsApp Morning Digest.

Pulls unread WhatsApp messages via Unipile, summarises with OpenAI, emails
via Gmail SMTP. Designed to run as a daily GitHub Actions cron.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests
from openai import OpenAI

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

UNREAD_LOOKBACK_HOURS = 36
WAITING_ON_YOU_DAYS = 7
WAITING_ON_THEM_DAYS = 3
SMALL_GROUP_MAX_MEMBERS = 15
MAX_MESSAGES_PER_CHAT = 30
MAX_AWAITING_ITEMS = 20
OPENAI_MODEL = "gpt-4o-mini"
CHAT_PAGE_LIMIT = 100

CHAT_FETCH_WINDOW_DAYS = 30
ACTIVELY_TALKING_HOURS = 2

HTTP_TIMEOUT = 30
RETRY_STATUSES = {429, 502, 503, 504}
RETRY_ATTEMPTS = 3

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def env(name: str, default: str | None = None, required: bool = True) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        print(f"❌ Missing required environment variable: {name}")
        sys.exit(1)
    return value or ""


UNIPILE_API_KEY = env("UNIPILE_API_KEY")
UNIPILE_DSN = env("UNIPILE_DSN")
UNIPILE_ACCOUNT_ID = env("UNIPILE_ACCOUNT_ID")
OPENAI_API_KEY = env("OPENAI_API_KEY")
GMAIL_ADDRESS = env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = env("RECIPIENT_EMAIL", default=GMAIL_ADDRESS, required=False) or GMAIL_ADDRESS

UNIPILE_BASE = f"https://{UNIPILE_DSN}/api/v1"
UNIPILE_HEADERS = {"X-API-KEY": UNIPILE_API_KEY, "Accept": "application/json"}

# --------------------------------------------------------------------------- #
# Unipile HTTP
# --------------------------------------------------------------------------- #


def unipile_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{UNIPILE_BASE}{path}"
    backoff = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=UNIPILE_HEADERS, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == RETRY_ATTEMPTS:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code in RETRY_STATUSES and attempt < RETRY_ATTEMPTS:
            time.sleep(backoff)
            backoff *= 2
            continue

        resp.raise_for_status()
        return resp.json()

    if last_exc:
        raise last_exc
    raise RuntimeError("Unreachable retry exit")


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def hours_since(iso_ts: str | None) -> float:
    dt = parse_iso(iso_ts)
    if dt is None:
        return float("inf")
    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def relative_time(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))}m"
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


# --------------------------------------------------------------------------- #
# Chat helpers
# --------------------------------------------------------------------------- #


def is_group(chat: dict[str, Any]) -> bool:
    return chat.get("type") == 1


def should_skip(chat: dict[str, Any]) -> bool:
    if chat.get("archived"):
        return True
    if chat.get("muted_until"):
        return True
    if chat.get("read_only") in (1, 2):
        return True
    return False


def chat_name(chat: dict[str, Any]) -> str:
    name = chat.get("name")
    if name:
        return name
    identifier = chat.get("attendee_public_identifier") or ""
    if "@" in identifier:
        phone = identifier.split("@", 1)[0]
        if phone.isdigit():
            return f"+{phone}"
        return phone or "Unknown"
    return "Unknown"


def format_message(msg: dict[str, Any], include_sender: bool = True) -> str:
    text = msg.get("text")
    if not text:
        mtype = msg.get("message_type") or "MESSAGE"
        text = f"[{mtype}]"
    text = text.strip()
    if len(text) > 400:
        text = text[:397].rstrip() + "…"
    sender = msg.get("sender_attendee_name")
    if include_sender and sender and not msg.get("is_sender"):
        return f"{sender}: {text}"
    return text


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #


def fetch_chats(window_hours: float) -> list[dict[str, Any]]:
    """Paginate /chats until we pass the lookback window."""
    all_chats: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        params: dict[str, Any] = {
            "account_id": UNIPILE_ACCOUNT_ID,
            "limit": CHAT_PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        data = unipile_get("/chats", params=params)
        items = data.get("items", []) or []
        if not items:
            break
        all_chats.extend(items)
        last_ts = items[-1].get("timestamp")
        last_hours = hours_since(last_ts)
        cursor = data.get("cursor")
        if last_hours > window_hours:
            break
        if not cursor:
            break
    print(f"   · fetched {len(all_chats)} chats across {page} page(s)")
    return all_chats


def fetch_messages(chat_id: str, limit: int) -> list[dict[str, Any]]:
    data = unipile_get(f"/chats/{chat_id}/messages", params={"limit": limit})
    return data.get("items", []) or []


_attendee_cache: dict[str, int] = {}


def attendee_count(chat_id: str) -> int:
    if chat_id in _attendee_cache:
        return _attendee_cache[chat_id]
    try:
        data = unipile_get(f"/chats/{chat_id}/attendees")
        count = len(data.get("items", []) or [])
    except requests.HTTPError:
        count = 0
    _attendee_cache[chat_id] = count
    return count


def group_size_class(chat: dict[str, Any]) -> str:
    """Return 'dm', 'small_group', or 'large_group'."""
    if not is_group(chat):
        return "dm"
    count = attendee_count(chat["id"])
    if count and count > SMALL_GROUP_MAX_MEMBERS:
        return "large_group"
    return "small_group"


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #


def build_unread_data(chats: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    dms: list[dict[str, Any]] = []
    small_groups: list[dict[str, Any]] = []
    large_groups: list[dict[str, Any]] = []

    for chat in chats:
        if should_skip(chat):
            continue
        unread = chat.get("unread_count") or 0
        if unread <= 0:
            continue
        if hours_since(chat.get("timestamp")) > UNREAD_LOOKBACK_HOURS:
            continue

        to_fetch = min(unread, MAX_MESSAGES_PER_CHAT)
        messages = fetch_messages(chat["id"], to_fetch)

        filtered: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("is_sender") == 1:
                continue
            if hours_since(msg.get("timestamp")) > UNREAD_LOOKBACK_HOURS:
                continue
            filtered.append(msg)

        if not filtered:
            continue

        filtered.reverse()  # chronological

        entry = {
            "name": chat_name(chat),
            "unread_count": unread,
            "messages": [format_message(m) for m in filtered],
        }

        klass = group_size_class(chat)
        if klass == "dm":
            dms.append(entry)
        elif klass == "small_group":
            small_groups.append(entry)
        else:
            entry["message_count"] = len(filtered)
            large_groups.append(entry)

    return {"dms": dms, "small_groups": small_groups, "large_groups": large_groups}


def build_waiting_on_you(chats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for chat in chats:
        if should_skip(chat):
            continue
        age_h = hours_since(chat.get("timestamp"))
        if age_h > WAITING_ON_YOU_DAYS * 24:
            continue

        if is_group(chat) and group_size_class(chat) == "large_group":
            continue

        messages = fetch_messages(chat["id"], 8)
        if not messages:
            continue

        newest = messages[0]
        if newest.get("is_sender") == 1:
            continue

        newest_age = hours_since(newest.get("timestamp"))
        if newest_age < ACTIVELY_TALKING_HOURS:
            continue

        chronological = list(reversed(messages))
        items.append(
            {
                "name": chat_name(chat),
                "age": relative_time(newest_age),
                "age_hours": newest_age,
                "is_group": is_group(chat),
                "recent_messages": [format_message(m) for m in chronological],
            }
        )
        if len(items) >= MAX_AWAITING_ITEMS:
            break

    items.sort(key=lambda x: x["age_hours"])
    for it in items:
        it.pop("age_hours", None)
    return items


def build_waiting_on_them(chats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for chat in chats:
        if should_skip(chat):
            continue
        age_h = hours_since(chat.get("timestamp"))
        if age_h > CHAT_FETCH_WINDOW_DAYS * 24:
            continue

        if is_group(chat) and group_size_class(chat) == "large_group":
            continue

        messages = fetch_messages(chat["id"], 8)
        if not messages:
            continue

        newest = messages[0]
        if newest.get("is_sender") != 1:
            continue

        newest_age = hours_since(newest.get("timestamp"))
        if newest_age < WAITING_ON_THEM_DAYS * 24:
            continue

        chronological = list(reversed(messages))
        items.append(
            {
                "name": chat_name(chat),
                "age": relative_time(newest_age),
                "age_hours": newest_age,
                "is_group": is_group(chat),
                "recent_messages": [format_message(m) for m in chronological],
            }
        )
        if len(items) >= MAX_AWAITING_ITEMS:
            break

    items.sort(key=lambda x: x["age_hours"])
    for it in items:
        it.pop("age_hours", None)
    return items


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You produce a concise morning WhatsApp digest as Gmail-safe HTML.

You receive four JSON lists describing the user's WhatsApp state:
- reply_today: unread DMs and small-group messages — pick only urgent items (direct questions, blockers, time-sensitive asks, decisions needed)
- waiting_on_you: chats where the other party sent the last message and the user hasn't replied
- waiting_on_them: chats where the user sent the last message 3+ days ago with no reply
- group_activity: large group chats with unread activity

Rules:
- Output ONLY HTML body content (no <html>, <head>, <body>, no preamble, no sign-off).
- Start directly with the first <p class="label">.
- Skip any section with zero qualifying items — do not emit empty headers.
- Filter noise: ignore "ok", "thanks", emoji-only reactions, acknowledgements, anything already handled.
- Do not emit "(No reply needed)" entries — just omit them.
- One bullet per item. "Waiting on you" items get one extra line for the suggested reply.
- Suggested replies: 1–2 sentences, casual, match conversation tone, no corporate speak.
- If a suggested reply needs info only the user has, write: → Need your input: [what].
- For group_activity, one-line vibe summary per group; flag if the user was tagged/mentioned.

Exact section template (use these labels verbatim):

<p class="label">REPLY TODAY</p>
<ul>
  <li><strong>{Name}</strong> — {one-line summary}</li>
</ul>

<p class="label">WAITING ON YOU</p>
<ul>
  <li>
    <strong>{Name}</strong> <span class="age">· {age}</span> — {one-liner}<br>
    <span class="reply">→ {suggested reply}</span>
  </li>
</ul>

<p class="label">WAITING ON THEM</p>
<ul>
  <li><strong>{Name}</strong> <span class="age">· {age}</span> — {what to follow up about}</li>
</ul>

<p class="label">GROUP ACTIVITY</p>
<ul>
  <li><strong>{Group}</strong> <span class="age">· {N} msgs</span> — {vibe, flag if tagged}</li>
</ul>
"""


def call_openai(
    unread: dict[str, list[dict[str, Any]]],
    waiting_on_you: list[dict[str, Any]],
    waiting_on_them: list[dict[str, Any]],
) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    reply_today_pool = {
        "dms": unread.get("dms", []),
        "small_groups": unread.get("small_groups", []),
    }
    group_activity = [
        {"name": g["name"], "message_count": g.get("message_count", g.get("unread_count", 0))}
        for g in unread.get("large_groups", [])
    ]

    payload = {
        "reply_today": reply_today_pool,
        "waiting_on_you": waiting_on_you,
        "waiting_on_them": waiting_on_them,
        "group_activity": group_activity,
    }

    user_message = (
        "Produce the digest HTML for the following data. Skip any empty sections.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    return (response.choices[0].message.content or "").strip()


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

EMAIL_SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#fff;">
  <div style="max-width:640px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#222;">
    <div style="font-size:13px;color:#888;margin-bottom:4px;letter-spacing:0.02em;">{date_line}</div>
    <div style="font-size:13px;color:#888;margin-bottom:28px;">{stats_line}</div>
    <style>
      .label {{ font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:#888;margin:24px 0 8px 0;font-weight:600; }}
      ul {{ margin:0 0 16px 0;padding-left:20px; }}
      li {{ margin:0 0 10px 0; }}
      .age {{ font-size:13px;color:#aaa; }}
      .reply {{ color:#555; }}
      strong {{ color:#111; }}
      a {{ color:#222; }}
    </style>
    <div class="digest-body">
{body}
    </div>
    <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px 0;">
    <div style="font-size:12px;color:#aaa;line-height:1.5;">
      AI-generated summary. May miss context or misread tone — check the thread before acting on anything important.
    </div>
  </div>
</body>
</html>
"""


def render_email(
    subject: str,
    body_html: str,
    waiting_on_you_count: int,
    waiting_on_them_count: int,
) -> str:
    now = datetime.now()
    date_line = now.strftime("%A %d %B %Y · %H:%M")
    stats_bits: list[str] = []
    if waiting_on_you_count:
        stats_bits.append(f"{waiting_on_you_count} waiting on you")
    if waiting_on_them_count:
        stats_bits.append(f"{waiting_on_them_count} waiting on them")
    stats_line = " · ".join(stats_bits) if stats_bits else "Nothing urgent"
    return EMAIL_SHELL.format(
        subject=subject,
        date_line=date_line,
        stats_line=stats_line,
        body=body_html,
    )


def send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText("This digest is HTML. View in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

INBOX_ZERO_BODY = """
<p style="margin:24px 0;color:#444;">Inbox zero. Nothing needs your attention.</p>
"""


def main() -> None:
    print("📥 Fetching chats…")
    chats = fetch_chats(CHAT_FETCH_WINDOW_DAYS * 24)

    print("📨 Building unread data…")
    unread = build_unread_data(chats)
    print(
        f"   · DMs: {len(unread['dms'])} · small groups: {len(unread['small_groups'])} "
        f"· large groups: {len(unread['large_groups'])}"
    )

    print("⏳ Building waiting-on-you…")
    waiting_on_you = build_waiting_on_you(chats)
    print(f"   · {len(waiting_on_you)} items")

    print("👻 Building waiting-on-them…")
    waiting_on_them = build_waiting_on_them(chats)
    print(f"   · {len(waiting_on_them)} items")

    all_empty = (
        not unread["dms"]
        and not unread["small_groups"]
        and not unread["large_groups"]
        and not waiting_on_you
        and not waiting_on_them
    )

    today = datetime.now().strftime("%a %d %B")
    subject = f"WhatsApp digest — {today}"

    if all_empty:
        print("✉️  Inbox zero — sending minimal digest.")
        html = render_email(subject, INBOX_ZERO_BODY, 0, 0)
        send_email(subject, html)
        print("✅ Done.")
        return

    print("🤖 Summarising…")
    body_html = call_openai(unread, waiting_on_you, waiting_on_them)

    if not body_html:
        print("⚠️  OpenAI returned empty body — falling back to inbox zero message.")
        body_html = INBOX_ZERO_BODY

    html = render_email(subject, body_html, len(waiting_on_you), len(waiting_on_them))

    print("✉️  Sending email…")
    send_email(subject, html)
    print("✅ Done.")


if __name__ == "__main__":
    main()
